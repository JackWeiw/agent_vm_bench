#include <iostream>
#include <thread>
#include <vector>
#include <chrono>
#include <cstring>
#include <atomic>
#include <signal.h>
#include <random>
#include <cstdlib>

// Compile: g++ -O2 -o stress_tool stress_tool.cpp -lpthread
// Usage: ./stress_tool -d 60 -m 1024 -c 4 -i 5

#include <csetjmp>

std::atomic<bool> g_running{true};
std::atomic<bool> g_crashed{false};
std::atomic<int> g_exit_code{0};
std::jmp_buf g_jump_buffer;

void signal_handler(int) {
    g_running = false;
}

// Crash signal handler - catch fatal signals and attempt graceful exit
void crash_handler(int sig) {
    g_crashed = true;
    g_exit_code = 128 + sig;

    // Output crash info to stderr
    std::cerr << "[CRASH] Caught signal " << sig << " (";
    switch(sig) {
        case SIGSEGV: std::cerr << "SIGSEGV/Segmentation fault"; break;
        case SIGABRT: std::cerr << "SIGABRT/Aborted"; break;
        case SIGILL: std::cerr << "SIGILL/Illegal instruction"; break;
        case SIGBUS: std::cerr << "SIGBUS/Bus error"; break;
        case SIGFPE: std::cerr << "SIGFPE/Floating point exception"; break;
        default: std::cerr << "Unknown signal";
    }
    std::cerr << ")" << std::endl;

    // Attempt graceful termination
    g_running = false;

    // Use longjmp to attempt recovery, if jump point is set
    if (sig != SIGABRT) {  // Do not execute longjmp for SIGABRT, avoid infinite loop
        std::longjmp(g_jump_buffer, 1);
    }
}

// Lightweight compute task - control recursion depth to avoid full load
long long light_compute(int n) {
    if (n <= 1) return n;
    return light_compute(n - 1) + light_compute(n - 2);
}

// CPU worker thread - duty cycle control, avoid full load
void cpu_worker(int intensity) {
    // intensity 1-10 controls CPU usage percentage (approximate)
    // intensity 3 = 30% CPU, intensity 5 = 50% CPU, intensity 8 = 80% CPU
    int work_percent = std::min(100, intensity * 10);
    int work_ms = work_percent;  // Work time in each 100ms cycle
    int sleep_ms = 100 - work_ms;

    // Limit recursion depth, single compute ~ 0.1-0.5ms, avoid single occupation too long
    const int fib_depth = 20;

    while (g_running) {
        auto cycle_start = std::chrono::steady_clock::now();

        // Work phase: intensive compute for specified time
        while (g_running) {
            volatile long long result = light_compute(fib_depth);
            (void)result; // Prevent optimization

            auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - cycle_start).count();
            if (elapsed >= work_ms) break;
        }

        // Sleep phase: actively yield CPU
        if (sleep_ms > 0 && g_running) {
            std::this_thread::sleep_for(std::chrono::milliseconds(sleep_ms));
        }
    }
}

// Memory worker thread - actual capacity occupation, minimal bandwidth
void memory_worker(size_t memory_mb, int duration_sec) {
    size_t total_bytes = memory_mb * 1024 * 1024;
    const size_t block_size = 64 * 1024 * 1024; // 64MB per block
    size_t num_blocks = (total_bytes + block_size - 1) / block_size;

    std::vector<char*> blocks;
    blocks.reserve(num_blocks);

    std::cout << "[Memory] Starting allocation " << memory_mb << " MB (sparse touch mode)..." << std::endl;

    // Phase 1: Allocate and sparse write (only touch first byte of each page, ensure physical allocation)
    // 4KB page size, write 1 byte per page, write bandwidth reduced to 1/4096
    for (size_t i = 0; i < num_blocks && g_running; ++i) {
        char* block = new char[block_size];

        // Sparse touch: write first byte every 4KB, trigger physical page allocation
        for (size_t j = 0; j < block_size; j += 4096) {
            block[j] = static_cast<char>(i % 256);
        }

        blocks.push_back(block);

        // Output progress every 512MB allocation, avoid long unresponsive time
        if ((i + 1) % 8 == 0) {
            std::cout << "[Memory] Allocated " << ((i + 1) * 64) << " MB..." << std::endl;
        }
    }

    if (!g_running) {
        for (auto* block : blocks) delete[] block;
        return;
    }

    std::cout << "[Memory] Allocation complete, " << blocks.size() << " blocks, total "
              << memory_mb << " MB. Entering low-frequency keepalive mode (access 1 byte every 100ms)..." << std::endl;

    // Phase 2: Low-frequency keepalive - access 1 random byte every 100ms, bandwidth negligible
    // Purpose: prevent memory from being swapped out by system, but almost no bandwidth pressure
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> block_dist(0, num_blocks - 1);
    std::uniform_int_distribution<> offset_dist(0, block_size - 1);

    auto start = std::chrono::steady_clock::now();
    size_t access_count = 0;

    while (g_running) {
        // Sleep 100ms, control check frequency
        std::this_thread::sleep_for(std::chrono::milliseconds(100));

        // Extremely sparse access: read 1 byte (prevent being optimized out)
        if (!blocks.empty()) {
            int b = block_dist(gen);
            int off = offset_dist(gen);
            volatile char dummy = blocks[b][off];
            (void)dummy;
            access_count++;
        }

        // Check total duration
        auto now = std::chrono::steady_clock::now();
        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - start).count();
        if (elapsed >= duration_sec) {
            break;
        }
    }

    std::cout << "[Memory] Keepalive access count: " << access_count
              << " (approx once every 100ms), starting memory release..." << std::endl;

    // Phase 3: Cleanup
    for (auto* block : blocks) {
        delete[] block;
    }

    std::cout << "[Memory] Memory release complete" << std::endl;
}

int main(int argc, char* argv[]) {
    // Default parameters
    int duration_sec = 60;
    size_t memory_mb = 1024;
    int cpu_threads = 4;
    int cpu_intensity = 5;      // Default 50% CPU usage

    // Parse parameters
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if ((arg == "-d" || arg == "--duration") && i + 1 < argc) {
            duration_sec = std::stoi(argv[++i]);
        } else if ((arg == "-m" || arg == "--memory") && i + 1 < argc) {
            memory_mb = std::stoll(argv[++i]);
        } else if ((arg == "-c" || arg == "--cpu") && i + 1 < argc) {
            cpu_threads = std::stoi(argv[++i]);
        } else if ((arg == "-i" || arg == "--intensity") && i + 1 < argc) {
            cpu_intensity = std::stoi(argv[++i]);
            if (cpu_intensity < 1) cpu_intensity = 1;
            if (cpu_intensity > 10) cpu_intensity = 10;
        } else if (arg == "-h" || arg == "--help") {
            std::cout << "Usage: " << argv[0] << " [options]\n"
                      << "Options:\n"
                      << "  -d, --duration <sec>    Runtime duration in seconds (default: 60)\n"
                      << "  -m, --memory <MB>       Memory to allocate in MB (default: 1024)\n"
                      << "  -c, --cpu <threads>     Number of CPU threads (default: 4)\n"
                      << "  -i, --intensity <1-10>  CPU duty cycle percent: 1=10%, 5=50%, 10=100% (default: 5)\n"
                      << "  -h, --help              Show this help\n"
                      << "\n"
                      << "Examples:\n"
                      << "  # 50% CPU, 2GB memory, run for 2 minutes\n"
                      << "  " << argv[0] << " -d 120 -m 2048 -c 4 -i 5\n"
                      << "\n"
                      << "  # Low load mode: 30% CPU, 4GB memory\n"
                      << "  " << argv[0] << " -d 60 -m 4096 -c 2 -i 3\n";
            return 0;
        }
    }

    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    int cpu_percent = cpu_intensity * 10;

    std::cout << "========================================" << std::endl;
    std::cout << "Stress Tool Started (Low Pressure Mode)" << std::endl;
    std::cout << "Duration:    " << duration_sec << " seconds" << std::endl;
    std::cout << "Memory:      " << memory_mb << " MB (sparse allocation)" << std::endl;
    std::cout << "CPU Threads: " << cpu_threads << " @ " << cpu_percent << "% duty cycle" << std::endl;
    std::cout << "Bandwidth:   Minimal (touch once per 4KB page, then idle)" << std::endl;
    std::cout << "========================================" << std::endl;

    // Set crash signal handlers (before starting threads)
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    signal(SIGSEGV, crash_handler);
    signal(SIGABRT, crash_handler);
    signal(SIGILL, crash_handler);
    signal(SIGBUS, crash_handler);
    signal(SIGFPE, crash_handler);

    auto start = std::chrono::steady_clock::now();

    // Use setjmp/longjmp to provide crash recovery mechanism
    if (setjmp(g_jump_buffer) == 0) {
        // Normal execution path

        // Start CPU threads
        std::vector<std::thread> cpu_workers;
        for (int i = 0; i < cpu_threads; ++i) {
            cpu_workers.emplace_back(cpu_worker, cpu_intensity);
        }

        // Start memory thread (execute in main thread)
        std::thread mem_thread(memory_worker, memory_mb, duration_sec);

        // Wait for specified time
        std::this_thread::sleep_for(std::chrono::seconds(duration_sec));

        // Stop signal
        g_running = false;

        // Wait for all threads to finish
        for (auto& t : cpu_workers) {
            if (t.joinable()) t.join();
        }
        if (mem_thread.joinable()) mem_thread.join();

    } else {
        // Crash recovery path (reached via longjmp)
        std::cerr << "[CRASH] Recovered from crash, performing cleanup..." << std::endl;
        // Cleanup resources...
    }

    auto end = std::chrono::steady_clock::now();
    auto actual_duration = std::chrono::duration_cast<std::chrono::seconds>(end - start).count();

    // Output structured log (JSON format), easy to parse
    std::cout << "{"
              << "\"event\":\"finish\","
              << "\"timestamp\":" << std::chrono::duration_cast<std::chrono::milliseconds>(
                     std::chrono::system_clock::now().time_since_epoch()).count() << ","
              << "\"duration_sec\":" << actual_duration << ","
              << "\"crashed\":" << (g_crashed ? "true" : "false") << ","
              << "\"exit_code\":" << (g_crashed ? g_exit_code.load() : 0)
              << "}" << std::endl;

    std::cout << "========================================" << std::endl;
    std::cout << "Stress Tool Finished" << std::endl;
    if (g_crashed) {
        std::cout << "EXIT_STATUS: CRASHED (signal " << g_exit_code << ")" << std::endl;
    } else {
        std::cout << "EXIT_STATUS: OK" << std::endl;
    }
    std::cout << "Actual duration: " << actual_duration << " seconds" << std::endl;
    std::cout << "Memory remained allocated with minimal bandwidth usage" << std::endl;
    std::cout << "========================================" << std::endl;

    return g_crashed ? g_exit_code.load() : 0;
}

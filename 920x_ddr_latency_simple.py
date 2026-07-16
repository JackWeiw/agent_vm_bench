import os
import subprocess
import sys


def find_files_with_string(directory, search_string):
    result = []
    # 递归查找指定目录下的所有文件
    for item in os.listdir(directory):
        # print (item)
        if search_string in item:
            result.append(str(item))
    return result


def get_ddr_stats(ddr_device):
    try:
        # 执行 perf 命令来测量 ddr-load-misses 和 ddr-loads
        event = ddr_device + "/config=0x00/,"
        event += ddr_device + "/config=0x41/,"
        event += ddr_device + "/config=0x44/,"
        event += ddr_device + "/config=0x80/,"
        event += ddr_device + "/config=0x81/,"
        event += ddr_device + "/config=0x83/,"
        event += ddr_device + "/config=0x84/"
        command = ["perf", "stat", "-e", event, "-o", "perf_output_ddr.txt", "sleep", "1"]
        # print (command)
        subprocess.run(command, check=True)

        # 读取 perf 输出文件
        with open("perf_output_ddr.txt") as f:
            lines = f.readlines()

        ddr_cycles = None
        ddr_rd = None
        ddr_wr = None
        ddr_rd_time = None
        ddr_wr_time = None
        ddr_rd_data = None
        ddr_wr_data = None

        # 解析 perf 输出，提取 ddr-load-misses 和 ddr-loads
        for line in lines:
            if "config=0x00" in line:
                parts = line.strip().split()
                ddr_cycles = int(float(parts[0].replace(",", "")))
            elif "config=0x41" in line:
                parts = line.strip().split()
                ddr_rd = int(float(parts[0].replace(",", "")))
            elif "config=0x44" in line:
                parts = line.strip().split()
                ddr_wr = int(float(parts[0].replace(",", "")))
            elif "config=0x80" in line:
                parts = line.strip().split()
                ddr_rd_time = int(float(parts[0].replace(",", "")))
            elif "config=0x81" in line:
                parts = line.strip().split()
                ddr_wr_time = int(float(parts[0].replace(",", "")))
            elif "config=0x84" in line:
                parts = line.strip().split()
                ddr_rd_data = int(float(parts[0].replace(",", "")))
            elif "config=0x83" in line:
                parts = line.strip().split()
                ddr_wr_data = int(float(parts[0].replace(",", "")))

        if ddr_cycles is not None:
            return ddr_cycles, ddr_rd + 1, ddr_wr + 1, ddr_rd_time, ddr_wr_time, ddr_rd_data, ddr_wr_data
        else:
            print("Failed to extract ddr-load-misses or ddr-loads from perf output.")
            return None, None, None

    except subprocess.CalledProcessError as e:
        print(f"Error running perf command: {e}")
        return None, None, None
    except FileNotFoundError:
        print("Perf output file not found.")
        return None, None, None


if __name__ == "__main__":
    usage = """
    help info:
    1) single DDR 
        python 920x_ddr_latency.py DDRName
        example: python 920x_ddr_latency.py hisi_sccl3_ddrc0_0
    2) all DDR 
        python 920x_ddr_latency.py
    """

    print(usage)

    ddr_name = ""
    if len(sys.argv) > 1:
        ddr_name = sys.argv[1]

    all_ddr = find_files_with_string("/sys/devices/", "ddrc")
    all_ddr.sort()
    while True:
        for DDR in all_ddr:
            if ddr_name.strip() != "":
                DDR = ddr_name
            ddr_cycles, ddr_rd, ddr_wr, ddr_rd_time, ddr_wr_time, ddr_rd_data, ddr_wr_data = get_ddr_stats(DDR)
            print("DDR Name".ljust(len(DDR)), end="  ")
            print("Frequency".ljust(len("Frequency")), end="  ")
            print("RD Bandwith(GB/s)".ljust(len("RD bandwith(GB/s)")), end="  ")
            print("WR Bandwith(GB/s)".ljust(len("WR bandwith(GB/s)")), end="  ")
            print("RD Latency(cycle)".ljust(len("RD Latency(cycle)")), end="  ")
            print("WR Latency(cycle)".ljust(len("WR Latency(cycle)")), end="  ")
            print()
            print(str(DDR).ljust(len("DDR Name")), end="  ")
            print(str("%.0f" % (ddr_cycles * 4 / 1000 / 1000)).ljust(len("Frequency")), end="  ")
            print(str("%.2f" % (ddr_rd_data * 32 / 1024 / 1024 / 1024)).ljust(len("RD bandwith(GB/s)")), end="  ")
            print(str("%.2f" % (ddr_wr_data * 32 / 1024 / 1024 / 1024)).ljust(len("WR bandwith(GB/s)")), end="  ")
            print(str("%.2f" % (ddr_rd_time / ddr_rd)).ljust(len("RD Latency(cycle)")), end="  ")
            print(str("%.2f" % (ddr_wr_time / ddr_wr)).ljust(len("WR Latency(cycle)")), end="  ")
            print()
            print()

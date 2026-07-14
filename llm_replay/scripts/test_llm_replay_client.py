#!/usr/bin/env python3
"""
test_llm_replay_client.py - Test client for LLM Replay Server.

Usage:
    python test_llm_replay_client.py
"""

import json
import requests
import time

BASE_URL = "http://127.0.0.1:5199"


def test_health():
    """Test health endpoint."""
    resp = requests.get(f"{BASE_URL}/health")
    print(f"[Health] Status: {resp.status_code}")
    print(json.dumps(resp.json(), indent=2))
    print()


def test_models():
    """Test models endpoint."""
    resp = requests.get(f"{BASE_URL}/v1/models")
    print(f"[Models] Status: {resp.status_code}")
    data = resp.json()
    print(f"Available models: {[m['id'] for m in data['data']]}")
    print()


def test_sessions():
    """Test sessions endpoint."""
    resp = requests.get(f"{BASE_URL}/v1/sessions")
    print(f"[Sessions] Status: {resp.status_code}")
    data = resp.json()
    for s in data["sessions"]:
        print(f"  - {s['name']}: {s['total_turns']} turns, {s['total_tool_calls']} tool_calls")
    print()


def test_chat_completion(model: str, num_turns: int = 3):
    """Test chat completion endpoint for multiple turns."""
    print(f"[Chat] Testing {num_turns} turns with model '{model}'")

    messages = []

    for turn in range(num_turns):
        # Add user message
        messages.append({"role": "user", "content": f"Test message {turn}"})

        print(f"  Turn {turn}: Requesting...")

        start_time = time.time()
        resp = requests.post(
            f"{BASE_URL}/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
            },
            timeout=60,
        )
        elapsed = time.time() - start_time

        print(f"    Status: {resp.status_code}, Time: {elapsed:.2f}s")

        if resp.status_code != 200:
            print(f"    Error: {resp.text}")
            continue

        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice["finish_reason"]

        print(f"    Finish reason: {finish_reason}")

        if message.get("content"):
            content_preview = message["content"][:50]
            print(f"    Content: {content_preview}...")

        if message.get("tool_calls"):
            tools = [tc["function"]["name"] for tc in message["tool_calls"]]
            print(f"    Tool calls: {tools}")

        # Add assistant response to messages for next turn
        messages.append(message)

        # If there are tool calls, add tool results
        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "Tool result placeholder"})

    print()


def test_streaming(model: str):
    """Test streaming chat completion."""
    print(f"[Stream] Testing streaming with model '{model}'")

    resp = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Stream test"}],
            "stream": True,
        },
        stream=True,
        timeout=60,
    )

    print(f"  Status: {resp.status_code}")

    chunks_received = 0
    for line in resp.iter_lines():
        if line:
            line = line.decode("utf-8")
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    print(f"  Stream complete, {chunks_received} chunks received")
                    break
                chunks_received += 1

    print()


def test_out_of_range(model: str, total_turns: int):
    """Test behavior when requesting beyond session length."""
    print(f"[OOB] Testing out-of-range (requesting turn {total_turns + 5})")

    # Build messages with many assistant responses
    messages = []
    for i in range(total_turns + 5):
        messages.append({"role": "user", "content": f"msg {i}"})
        messages.append({"role": "assistant", "content": f"response {i}"})

    # Remove last assistant to make request
    messages = messages[:-1]

    resp = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
        },
        timeout=60,
    )

    print(f"  Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        finish_reason = data["choices"][0].get("finish_reason")
        print(f"  Finish reason: {finish_reason} (expected: stop for OOB)")

    print()


def main():
    """Run all tests."""
    print("=" * 60)
    print("LLM Replay Server Test Client")
    print("=" * 60)
    print()

    try:
        # Basic endpoints
        test_health()
        test_models()
        test_sessions()

        # Get model name
        model = "browser-session-3"

        # Test chat completion
        test_chat_completion(model, num_turns=5)

        # Test streaming
        test_streaming(model)

        # Test out-of-range
        test_out_of_range(model, total_turns=99)

        print("=" * 60)
        print("All tests completed!")
        print("=" * 60)

    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot connect to server. Is it running?")
        print("Start with: python -m llm_replay --config llm_replay/config/llm_replay.yaml")


if __name__ == "__main__":
    main()

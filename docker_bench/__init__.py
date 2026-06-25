"""
Docker Container Bench - Browser Automation Performance Testing

Tests OpenClaw browser automation capability in Docker containerized deployment environment.
Measures QPS (queries per second) as core performance metric.

Browser Workflow (5 steps = 1 query):
  Step 1: openclaw browser open [URL] --label [NAME]  → Page open
  Step 2: openclaw browser focus [TAB_ID]             → Tab focus
  Step 3: openclaw browser snapshot --limit 200       → DOM snapshot
  Step 4: openclaw browser click e218                 → Element click (retry)
  Step 5: openclaw browser screenshot                 → Visual screenshot
"""

__version__ = "1.0.0"
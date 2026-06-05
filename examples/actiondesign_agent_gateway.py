#!/usr/bin/env python3
from claude_agent_sdk.actiondesign_gateway.app import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8888)

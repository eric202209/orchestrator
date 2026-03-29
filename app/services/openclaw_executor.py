"""
OpenClaw Executor - Execute tasks via OpenClaw's session system
"""

import httpx
import asyncio
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class OpenClawConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8000"
    timeout: float = 60.0


class OpenClawExecutor:
    """Execute tasks via OpenClaw's session system"""

    def __init__(self, config: Optional[OpenClawConfig] = None):
        self.config = config or OpenClawConfig()
        self.client = httpx.AsyncClient(
            base_url=self.config.base_url, timeout=self.config.timeout
        )

    async def execute_task(
        self, task_id: str, description: str, requirements: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Spawn an OpenClaw session to execute a task

        Args:
            task_id: Orchestrator task ID
            description: Task description
            requirements: Optional detailed requirements

        Returns:
            Session info with sessionKey for monitoring
        """
        # Build task prompt
        prompt = f"Execute task: {description}"
        if requirements:
            prompt += f"\n\nRequirements:\n{requirements}"

        # Spawn OpenClaw session
        payload = {
            "task": prompt,
            "label": f"task-{task_id}",
            "runtime": "acp",  # ACP harness for full tool access
            "agentId": "qwen",  # Your default model
            "timeoutSeconds": 3600,  # 1 hour max
            "mode": "run",  # One-shot execution
            "cleanup": "delete",  # Auto-cleanup after completion
        }

        response = await self.client.post("/api/v1/sessions/spawn", json=payload)

        if response.status_code != 200:
            raise Exception(f"Failed to spawn session: {response.text}")

        session_info = response.json()

        # Update orchestrator DB with session tracking
        await self._update_task_with_session(task_id, session_info)

        return session_info

    async def monitor_session(
        self, session_key: str, task_id: str, update_interval: float = 5.0
    ) -> Dict[str, Any]:
        """
        Monitor a session's progress and update orchestrator DB

        Args:
            session_key: OpenClaw session key
            task_id: Orchestrator task ID
            update_interval: How often to check status

        Returns:
            Final session result
        """
        while True:
            try:
                # Get session status
                status_response = await self.client.get(
                    f"/api/v1/sessions/{session_key}/status"
                )

                if status_response.status_code != 200:
                    await asyncio.sleep(update_interval)
                    continue

                status = status_response.json()

                # Update orchestrator DB
                await self._update_task_progress(
                    task_id,
                    output=status.get("lastMessage", ""),
                    usage=status.get("usage", {}),
                    model=status.get("model", ""),
                )

                # Check if completed
                if status.get("state") == "completed":
                    return status

                await asyncio.sleep(update_interval)

            except Exception as e:
                print(f"Error monitoring session: {e}")
                await asyncio.sleep(update_interval)

    async def _update_task_with_session(
        self, task_id: str, session_info: Dict[str, Any]
    ):
        """Update orchestrator DB with session tracking info"""
        # TODO: Implement DB update logic
        # await db.update_task(task_id, {
        #     "session_key": session_info["sessionKey"],
        #     "status": "In Progress",
        #     "started_at": datetime.utcnow()
        # })
        print(
            f"Would update task {task_id} with session {session_info.get('sessionKey')}"
        )

    async def _update_task_progress(
        self, task_id: str, output: str, usage: Dict, model: str
    ):
        """Update orchestrator DB with task progress"""
        # TODO: Implement DB update logic
        # await db.update_task(task_id, {
        #     "output": output,
        #     "token_usage": usage,
        #     "model": model
        # })
        print(f"Would update task {task_id} progress: {len(output)} chars")

    async def get_session_output(self, session_key: str) -> str:
        """Get final output from a completed session"""
        response = await self.client.get(f"/api/v1/sessions/{session_key}/history")

        if response.status_code != 200:
            raise Exception(f"Failed to get session history: {response.text}")

        history = response.json()
        return "\n".join([msg["content"] for msg in history.get("messages", [])])

    async def cancel_session(self, session_key: str) -> bool:
        """Cancel a running session"""
        response = await self.client.post(f"/api/v1/sessions/{session_key}/cancel")
        return response.status_code == 200

    async def close(self):
        """Close HTTP client"""
        await self.client.aclose()

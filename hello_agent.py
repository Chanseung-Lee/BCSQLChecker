import asyncio
from dotenv import load_dotenv
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage

load_dotenv()

async def main():
    async for message in query(
        prompt="Reply with exactly: SDK is working.",
        options=ClaudeAgentOptions(allowed_tools=[]),  # no tools needed for this test
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text"):
                    print(block.text)
        elif isinstance(message, ResultMessage):
            print(f"Done: {message.subtype}")

asyncio.run(main())
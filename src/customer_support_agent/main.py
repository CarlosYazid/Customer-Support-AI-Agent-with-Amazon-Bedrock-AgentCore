"""
Customer Support AI Agent
==========================================

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── App Initialisation ───────────────────────────────────────────────
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.

app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.

GATEWAY_URL = os.environ.get("GATEWAY_URL", "") # TODO: Replace with your Gateway URL
KB_ID       = os.environ.get("KB_ID", "") # TODO: Replace with your Knowledge Base ID
REGION      = os.environ.get("AWS_REGION", "") # TODO: Replace with your AWS region
MEMORY_ID   = os.environ.get("MEMORY_ID", "") # TODO: Replace with your Memory ID


# ── Model and Clients ────────────────────────────────────────────────
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION

model = BedrockModel(model_id="global.amazon.nova-2-lite-v1:0")
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── Namespace Helper ─────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    try:
        strategies = mem_client.get_memory_strategies(memory_id)
        return {
            strategy["type"]: strategy["namespaces"][0]
            for strategy in strategies
            if "type" in strategy and "namespaces" in strategy and strategy["namespaces"]
        }
    except Exception as e:
        logger.warning(f"Failed to fetch memory strategies: {e}")
        return {}


# ── Memory Hook ──────────────────────────────────────────────────────

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id) if memory_client and memory_id else {}

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        if not event.agent.messages:
            return

        last_message = event.agent.messages[-1]
        if getattr(last_message, "role", None) != "user" or not isinstance(last_message.content, str):
            return

        user_query = last_message.content
        memories = []

        for strat_type, ns_template in self.namespaces.items():
            try:
                namespace = ns_template.format(actorId=self.actor_id)
                retrieved = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=user_query,
                    top_k=5,
                )
                for mem in retrieved:
                    mem_text = mem.get("text") or mem.get("content")
                    if mem_text:
                        memories.append(f"[{strat_type}] {mem_text}")
            except Exception as e:
                logger.warning(f"Error retrieving memories for namespace {ns_template}: {e}")

        if memories:
            context_str = "\n".join(memories)
            last_message.content = f"Customer Context:\n{context_str}\n\n{user_query}"

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        user_query = None
        assistant_response = None

        for msg in reversed(event.agent.messages):
            if assistant_response is None and getattr(msg, "role", None) == "assistant" and isinstance(msg.content, str):
                assistant_response = msg.content
            elif user_query is None and getattr(msg, "role", None) == "user" and isinstance(msg.content, str):
                content = msg.content
                if "Customer Context:\n" in content and "\n\n" in content:
                    content = content.split("\n\n", 1)[1]
                user_query = content

            if user_query and assistant_response:
                break

        if user_query and assistant_response:
            try:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[(user_query, "USER"), (assistant_response, "ASSISTANT")],
                )
            except Exception as e:
                logger.warning(f"Failed to save support interaction to memory: {e}")

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks using add_callback."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)

# ── Knowledge Base Tool ─────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID or KB_ID == "<kbid>":
        return "Knowledge base not configured."

    try:
        response = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query}
        )
        
        results = response.get("retrievalResults", [])
        if not results:
            return "No relevant information found in the knowledge base."

        chunks = [res["content"]["text"] for res in results if "content" in res and "text" in res["content"]]
        return "\n---\n".join(chunks)
    except Exception as e:
        logger.error(f"Error querying knowledge base: {e}")
        return f"Error retrieving knowledge base data: {str(e)}"


# ── Loyalty Discount Tool (Code Interpreter) ────────────────────────

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
import json, math

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

pts = {loyalty_points}
tier = "{tier}"
total = {order_total}
cat = "{product_category}"

max_pts_allowed = math.floor((total * 0.50) * 100)
pts_to_use = min(pts - (pts % 500), max_pts_allowed)
pts_discount = pts_to_use / 100.0

subtotal_after_pts = total - pts_discount
tier_rate = tier_rates.get(tier, 0.0)
tier_discount = subtotal_after_pts * tier_rate

final_total = round(subtotal_after_pts - tier_discount, 2)
total_savings = round(pts_discount + tier_discount, 2)
pts_earned = math.floor(final_total * earn_rates.get(cat, 1))
remaining_points = pts - pts_to_use + pts_earned

result = {{
    "original_total": total,
    "points_redeemed": pts_to_use,
    "points_discount": round(pts_discount, 2),
    "tier_rate": float(tier_rate)
    "tier_discount": round(tier_discount, 2),
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": pts_earned,
    "remaining_points": remaining_points
}}

print(json.dumps(result))
"""

    try:
        with code_session(REGION) as session:
            exec_response = session.invoke("executeCode", {"code": code, "language": "python", "clearContext": True})
            
            for event in exec_response:
                if "executionOutput" in event:
                    return event["executionOutput"].get("stdout", json.dumps(event))
                elif "stdout" in event:
                    return str(event["stdout"])
            return json.dumps(exec_response)

    except Exception as e:
        logger.warning(f"Code Interpreter execution failed, applying fallback calculations: {e}")
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_rate = tier_rates.get(tier, 0.0)
        tier_discount = round(order_total * tier_rate, 2)
        final_total = round(order_total - tier_discount, 2)
        return json.dumps({
            "fallback": True,
            "original_total": order_total,
            "tier_discount": tier_discount,
            "final_total": final_total,
            "note": "Calculated using tier discount fallback."
        })


# ── Agent Entrypoint ─────────────────────────────────────────────────

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        if isinstance(payload, str):
            payload = json.loads(payload)

        prompt = payload.get("prompt", "")
        actor_id = payload.get("customer_id") or payload.get("actor_id", "CUST-DEFAULT")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        
        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = []

        if GATEWAY_URL and GATEWAY_URL != "<gateway_url>":
            
            try:

                async with streamable_http_client(GATEWAY_URL) as (read_stream, write_stream):
                    
                    gateway_client = MCPClient(read_stream, write_stream)

                    async with gateway_client:
                        
                        gateway_tools = await gateway_client.get_tools()
                        tools.extend(gateway_tools)

                        logger.info("Gateway connected successfully. Loaded %d tools.", len(gateway_tools))

            except TimeoutError:
                logger.exception("Gateway tool loading timed out")

            except ConnectionError:
                logger.exception("Gateway connection failed")

            except Exception as e:
                logger.warning(f"Could not connect to Gateway MCP tools: {e}")

        tools.extend([search_knowledge_base, calculate_loyalty_discount, agent_core_browser.browser])

        system_prompt = (
            "You are a helpful customer support agent for an e-commerce platform. "
            "Use the provided knowledge base, loyalty discount calculator, browser, and MCP tools "
            "to accurately assist customers with their requests."
        )
        agent = Agent(
            model=model,
            tools=tools,
            state={"session_id": session_id, "actor_id": actor_id},
            hooks=[memory_hook],
            system_prompt=system_prompt,
        )

        response = await agent.invoke_async(prompt)

        if hasattr(response, "messages") and response.messages:
            last_msg = response.messages[-1]
            if hasattr(last_msg, "content"):
                if isinstance(last_msg.content, str):
                    return last_msg.content
                elif isinstance(last_msg.content, list) and len(last_msg.content) > 0:
                    first_block = last_msg.content[0]
                    if isinstance(first_block, dict):
                        return first_block.get("text", str(first_block))
                    return str(first_block)

        return str(response)

    except Exception as e:
        logger.error(f"Error during agent invocation: {e}", exc_info=True)
        return f"An error occurred while processing your request: {str(e)}"


# ── CLI entry point──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
# Customer Support AI Agent with Amazon Bedrock AgentCore

A production-style customer support agent for an e-commerce platform, built with the **Strands Agents SDK** and deployed on **Amazon Bedrock AgentCore**. The agent answers product and policy questions from a knowledge base, tracks orders, processes refunds, calculates loyalty discounts with exact arithmetic, and remembers each customer across sessions.

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Tech Stack](#tech-stack)
4. [Project Structure](#project-structure)
5. [How It Works](#how-it-works)
6. [Prerequisites](#prerequisites)
7. [Configuration](#configuration)
8. [Running Locally](#running-locally)
9. [Deploying to AgentCore](#deploying-to-agentcore)
10. [Request Format](#request-format)
11. [Sample Data](#sample-data)
12. [Production Considerations](#production-considerations)
13. [License](#license)

---

## Features

| Capability | Description | Powered by |
|---|---|---|
| **Knowledge base Q&A** | Product specs, return/refund policy, warranty, loyalty program and order-status definitions | Amazon Bedrock Knowledge Bases (`retrieve` API) |
| **Order tracking** | Look up an order, a customer's orders, or a customer profile | AWS Lambda behind AgentCore Gateway (API Gateway proxy integration) |
| **Refund management** | Initiate a refund, check refund status, generate a prepaid return label | AWS Lambda invoked directly by AgentCore Gateway |
| **Loyalty discounts** | Deterministic points and tier discount calculation | AgentCore Code Interpreter (sandboxed Python) |
| **Long-term memory** | Facts, preferences, summaries and episodes remembered per customer | AgentCore Memory (4 strategies) |
| **Web browsing** | Lets the agent consult web pages when needed | AgentCore Browser |
| **Observability** | Tracing support through the AWS OpenTelemetry distro | OpenTelemetry / CloudWatch |

## Architecture

```
                         ┌──────────────────────────────────────────┐
   Customer request ───▶ │   AgentCore Runtime (ARM64 container)    │
   {prompt, customer_id, │                                          │
    session_id}          │   Strands Agent  ── Amazon Nova 2 Lite   │
                         │     │                                    │
                         │     ├── MemoryHook ──▶ AgentCore Memory  │
                         │     ├── search_knowledge_base ──▶ Bedrock Knowledge Base
                         │     ├── calculate_loyalty_discount ──▶ AgentCore Code Interpreter
                         │     ├── browser ──▶ AgentCore Browser    │
                         │     └── MCP client ──▶ AgentCore Gateway │
                         └───────────────────────┬──────────────────┘
                                                 │
                              ┌──────────────────┴───────────────────┐
                              ▼                                      ▼
                   order_tracker Lambda                   refund_processor Lambda
                (via API Gateway proxy: GET            (direct Lambda target: initiate_refund,
                 /orders, /customers/...)               check_refund_status, get_return_label)
```

## Tech Stack

- **Language:** Python 3.13+
- **Agent framework:** [Strands Agents](https://strandsagents.com) (`strands-agents`, `strands-agents-tools`)
- **Runtime and platform services:** `bedrock-agentcore` (Runtime, Memory, Gateway, Code Interpreter, Browser)
- **Model:** `global.amazon.nova-2-lite-v1:0` via Amazon Bedrock
- **Tool protocol:** Model Context Protocol (MCP) over streamable HTTP
- **Packaging:** `uv` and a multi-stage Docker build targeting ARM64
- **Observability:** `aws-opentelemetry-distro`
- **Infrastructure config:** `agentcore.json` (managed by CDK)

## Project Structure

```
.
├── Dockerfile                         # Multi-stage ARM64 build for AgentCore
├── pyproject.toml / uv.lock           # Dependencies (managed with uv)
├── data/
│   └── product_catalog.txt            # Knowledge base source: products, return policy, loyalty program
├── docs/
│   └── Reflection.md                  # Reflections on the Project
├── media/images/                      # Screenshots of the deployment and testing
└── src/
    ├── customer_support_agent/
    │   └── main.py                    # Agent, tools, memory hook and AgentCore entrypoint
    └── lambdas/
        ├── order_tracker.py           # Order and customer lookup (REST proxy)
        ├── refund_processor.py        # Refund tools (direct Lambda target)
        └── lambda_schema.json         # Tool schema exposed through the Gateway
```

## How It Works

### 1. Agent entrypoint
`main.py` registers a `BedrockAgentCoreApp` with an async `@app.entrypoint`. On each request it parses the payload, builds a `MemoryHook` for the customer, connects to the Gateway over MCP to discover its tools, adds the local tools, and runs the Strands `Agent` with `invoke_async`.

### 2. Long-term memory
`MemoryHook` is a Strands `HookProvider` with two callbacks:

- **`MessageAddedEvent`:** retrieves the top 5 relevant memories from every configured strategy namespace and prepends them to the user message as `Customer Context`.
- **`AfterInvocationEvent`:** saves the completed user/assistant turn as an AgentCore Memory event. The injected context is stripped first so retrieved memories are never re-stored as if the customer had said them.

Memory is configured in `agentcore.json` with 30-day event expiry and four strategies:

| Strategy | Namespace |
|---|---|
| `SEMANTIC` | `/users/{actorId}/facts` |
| `USER_PREFERENCE` | `/users/{actorId}/preferences` |

All memory calls are wrapped in error handling so a memory failure never blocks a customer reply.

### 3. Gateway tools (MCP)

| Lambda | Integration | Tools / routes |
|---|---|---|
| `order_tracker` | API Gateway REST proxy | `GET /orders/{order_id}`, `GET /customers/{customer_id}/orders`, `GET /customers/{customer_id}` |
| `refund_processor` | Direct Lambda target | `initiate_refund`, `check_refund_status`, `get_return_label` |

The refund Lambda reads the tool name from the Lambda client context (`bedrockAgentCoreToolName`, formatted `TargetName___toolName`) and routes on the bare name. Tool arguments are declared in `lambda_schema.json`.

### 4. Knowledge base tool
`search_knowledge_base` calls the Bedrock Agent Runtime `retrieve` API and returns the matching chunks. The source document (`data/product_catalog.txt`) covers the product catalog, return and refund policy, the loyalty program and order status definitions.

### 5. Loyalty discount calculator
`calculate_loyalty_discount` runs the discount logic as Python inside the **AgentCore Code Interpreter** sandbox, so the numbers are computed exactly instead of being estimated by the model.

| Rule | Value |
|---|---|
| Point value | 100 points = $1 |
| Redemption | Blocks of 500 points, capped at 50% of the order total |
| Tier discount (applied after points) | Silver 0%, Gold 10%, Platinum 15% |
| Earn rate (points per $1) | Standard 1, Device 2, Fresh 5 |

If the sandbox call fails, the tool falls back to a simple tier discount and flags the result with `"fallback": true`.

## Prerequisites

- An AWS account with access to Amazon Bedrock (including the Nova 2 Lite model) and Bedrock AgentCore
- Python 3.13+ and [`uv`](https://docs.astral.sh/uv/)
- The AgentCore CLI (`agentcore`), installed through `bedrock-agentcore-starter-toolkit`
- AWS credentials configured locally (`aws configure` or environment variables)
- Provisioned AWS resources:
  - A Bedrock **Knowledge Base** loaded with `data/product_catalog.txt`
  - An AgentCore **Memory** resource (see `agentcore.json`)
  - An AgentCore **Gateway** with the two Lambda targets (`order_tracker`, `refund_processor`)

## Configuration

The agent reads its configuration from environment variables:

| Variable | Description |
|---|---|
| `GATEWAY_URL` | MCP endpoint of the AgentCore Gateway |
| `KB_ID` | Bedrock Knowledge Base ID |
| `AWS_REGION` | AWS region where the resources live |
| `MEMORY_ID` | AgentCore Memory ID |

If `GATEWAY_URL` or `KB_ID` is missing, the agent still starts and simply runs without those tools.

## Running Locally

```bash
# Install dependencies
uv sync

# Export configuration
export GATEWAY_URL="https://<your-gateway>/mcp"
export KB_ID="<knowledge-base-id>"
export MEMORY_ID="<memory-id>"
export AWS_REGION="us-east-1"

# Single invocation from the command line
uv run main.py '{"prompt": "Where is my order ORD-001?", "customer_id": "CUST-123", "session_id": "s1"}'
```

## Deploying to AgentCore

```bash
agentcore deploy

agentcore invoke '{"prompt": "I want a refund for ORD-002", "customer_id": "CUST-123", "session_id": "s1"}'
```

The included `Dockerfile` uses a two-stage build: dependencies are resolved by `uv` for `aarch64` in the builder stage, and only the virtual environment and `main.py` are copied into a slim Python 3.13 runtime image, since AgentCore Runtime runs on ARM64.

## Request Format

| Field | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | Yes | The customer's message |
| `customer_id` | string | No | Unique customer identifier, used as the memory `actor_id` (defaults to `CUST-DEFAULT`) |
| `session_id` | string | No | Conversation identifier (a UUID is generated if omitted) |

## Sample Data

The Lambdas ship with demo data so the agent can be tested end to end.

| Customer | Name | Tier | Points |
|---|---|---|---|
| `CUST-123` | Jane Smith | Gold | 4250 |
| `CUST-456` | Bob Johnson | Silver | 890 |

| Order | Customer | Status | Items |
|---|---|---|---|
| `ORD-001` | CUST-123 | SHIPPED | Wireless Headphones Pro |
| `ORD-002` | CUST-123 | DELIVERED | Kindle Paperwhite |
| `ORD-003` | CUST-456 | PROCESSING | 2x Echo Dot 5th Gen, Smart Plug |

Example prompts to try:

- "Where is my order ORD-001?"
- "What's the return policy for the Kindle Paperwhite?"
- "I'd like a refund for ORD-002 and a return label."
- "How much would I pay for a $200 order with my loyalty points?"

## Production Considerations

This project is a working demonstration. Before using it with real customers, address the following:

- **Authorization:** derive `customer_id` from a verified identity token rather than the request payload, and verify that every order or refund belongs to the authenticated customer.
- **Refund controls:** `initiate_refund` currently approves every request. Add validation against the order record, amount limits, and human approval above a threshold.
- **Input validation:** validate `tier` and `product_category` against allowed values before they are placed into the Code Interpreter script.
- **Real data stores:** replace the hard-coded order, customer and refund data with a database such as Amazon DynamoDB.
- **Cost and scalability:** reuse the browser tool and MCP connection across requests instead of creating them on every invocation.
- **Monitoring:** build CloudWatch dashboards and alarms on tool error rates and on how often the discount fallback is used.

## License

Distributed under the Apache License 2.0. See [`LICENSE`](LICENSE) for details.

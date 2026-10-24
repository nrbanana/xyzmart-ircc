**GCP Platform Engineering - Take-Home Case Study**

**Scenario: Automated Inventory Recovery & Customer Experience Agent
(xyzMart Corp)**

**1. Executive Summary & Context**

You are a Lead GCP Platform Engineer under the Accenture-Google Business
Group (AGBG). Your client, **xyzMart Corp** (a fictional, non-existent
retail enterprise), is a global retail giant with severe supply chain
and customer retention bottlenecks. When a high-demand item goes
out-of-stock (OOS) online, xyzMart currently loses the sale entirely and
suffers a drop in customer satisfaction index (CSI).

To resolve this, the AGBG team is building an **Autonomous Inventory
Recovery & Customer Compensation (IRCC) system**.

The solution centers on a generative-AI-powered agent that detects OOS
events via real-time telemetry, analyzes inventory alternatives,
initiates vendor backorders, and autonomously generates
hyper-personalized customer options (e.g., matching item suggestions,
custom discounts, or priority backorder reservations) depending on the
customer's loyalty profile.

――――――――――――――――――――――――――――――――――――――――――――――――――

**2. High-Level Requirements**

Your task is to design an architecture blueprint and build a lightweight
functional agent prototype that coordinates across three core pillars:

1.  **Agentic Data Cloud**: Unify raw transactional inventory data from
    Cloud SQL, structured analytics in BigQuery, and unstructured
    catalog/unstructured customer profiles grounded via Vertex AI
    Search.

2.  **Applications & Integrations**: Build an API-driven ecosystem.
    Integrate the agent with external vendor backordering systems and
    legacy ERP pipelines securely using Google Cloud's orchestration
    suite.

3.  **Gemini Enterprise Agent Platform (GE Agent Platform)**: Package,
    evaluate, and operate the agent within a centralized governance
    runtime, enabling security, observability, and evaluation-driven
    releases.

――――――――――――――――――――――――――――――――――――――――――――――――――

**3. Core Blueprint Tasks (Required for All Candidates)**

You must submit a technical architectural document and proto-code
covering the following horizontal areas:

***Task 3.1: Architecture & Topology Blueprint***

Provide an end-to-end system diagram showing how components talk to each
other across Google Cloud. Specify how a real-time Pub/Sub stockout
event routes through to agent initiation, grounding, multi-turn
reasoning, tool execution, and delivery back to the client interface.
Ensure you incorporate:

- VPC network architecture (e.g., Shared VPC structure, Private Google
  Access).

- Cloud Run / GKE runtime boundaries.

- Apigee API Management boundary.

- BigQuery and Vertex AI security layers.

***Task 3.2: Prototype Agent Implementation (ADK-preferred)***

Write an executable Python prototype demonstrating the agent's core
decision-making loop.

- We highly prefer implementation using the **GCP Agent Development Kit
  (ADK)** or Gemini Developer SDK. If you are more comfortable with an
  alternative framework (such as LangGraph or Vertex AI Agent Builder),
  you may use it, but state your architectural reasoning.

- The prototype must implement **Function Calling (Tool Use)** to pull
  real-time inventory from an external tool, check a customer's loyalty
  value, and submit a vendor backorder.

- Provide the complete Python file, including the system directives,
  state management, and error fallbacks.

***Task 3.3: GCP Project Access & Local Mocking Guidelines***

If you do not have active access to a Google Cloud Platform (GCP)
project or sandbox environment, you can fully implement and run this
coding/data prototype using mock local services. The evaluation honors
equivalent development patterns:

- **GCP Secret Manager Substitution**: Using local \`.env\`
  configuration files or an in-memory mock configuration secure
  key-store is fully sufficient.

- **Pub/Sub Message Substitution**: Real-time messaging loops can be
  replaced by local in-memory event emitters, simple Python asynchronous
  queues, or local lightweight brokers.

- **Dataflow Pipeline Substitution**: Apache Beam pipelines drafted for
  Dataflow can be fully executed and verified locally using the Apache
  Beam DirectRunner.

- **Database & Warehouse Mocking**: Local SQLite files or DuckDB
  instances can be substituted in place of Cloud SQL and BigQuery
  references for your code executions.

――――――――――――――――――――――――――――――――――――――――――――――――――

**4. Specialization Deep-Dives (Select ONE Track)**

To align with your primary role focus, you are required to expand
thoroughly on **ONE** of the following domains. Treat this section as a
deep structural defense of your solution:

***TRACK A: Agentic Data Cloud (Data Engineering Specialist)***

Focus on building a robust, real-time semantic grounding loop for
xyzMart's product and inventory data.

- **Grounding Pipeline**: Design an end-to-end ingestion pipeline
  transferring real-time inventory adjustments from transactional
  databases (Cloud SQL) to BigQuery using Datastream (CDC) and Dataflow.

- **Unstructured Grounding**: Detail how product catalog manuals and
  unstructured item descriptions are indexed, managed inside Vertex AI
  Search, and queried for semantic chunk retrieval. Explain document
  chunking strategies, metadata extraction, and indexing update
  intervals.

- **Semantic Layer & Data Governance**: Create the BigQuery table
  schemas, and model the semantic views that the Gemini agent will call
  via Text-to-SQL.

- **Security & Compliance**: Specify how row-level and column-level
  security policies are applied in BigQuery to prevent the agent from
  accessing restricted customer PII (e.g., payment numbers, internal
  wholesale costs).

***TRACK B: Applications & Integrations (App & System Integration
Specialist)***

Focus on the APIs, external system handshakes, and developer tooling
lifecycle.

- **Apigee API Gateway Management**: Design the Apigee proxy rules,
  rate-limiting, and OAuth2 security profiles wrapping the tools that
  the agent executes.

- **Model Context Protocol (MCP) Server**: Construct an MCP server
  schema or JSON-RPC spec exposing xyzMart's inventory search engine and
  backorder submission APIs to the Gemini Agent.

- **Event-Driven Chaining**: Structure the Pub/Sub topics, schemas, and
  Cloud Workflows orchestration to handle asynchronous backorder
  fulfillment loops and human-in-the-loop (HITL) approval gates.

- **Runtime Efficiency**: Detail execution scaling on Serverless
  runtimes (Cloud Run instance warm-up strategies, streaming gRPC/REST
  configurations, and context-caching optimizations to minimize
  round-trip latencies).

***TRACK C: Gemini Enterprise Agent Platform & Security (Platform /
DevSecOps Specialist)***

Focus on multi-tenant deployment, governance, continuous validation, and
absolute system isolation.

- **VPC Service Controls (VPC-SC)**: Draw or detail the security
  perimeter containing Vertex AI endpoints, BigQuery datasets, Cloud
  Run, and Secret Manager. How do you authorize secure egress to
  external vendor APIs (e.g., Salesforce, SAP)?

- **Continuous Evaluation Pipeline**: Define a complete GitOps CI/CD
  process (Cloud Build or GitHub Actions) that runs automated
  "LLM-as-a-judge" evaluation scripts testing the agent for OOS
  classification accuracy, grounding faithfulness, and toxic/jailbreak
  injections before promoting a model release.

- **Secret & Tenant Isolation**: Securely store API keys and database
  credentials in Secret Manager, utilizing KMS Customer-Managed
  Encryption Keys (CMEK) and IAM fine-grained privileges (Workload
  Identity Federation).

- **FinOps & Resource Allocation**: Outline concrete SLO/SLI metrics for
  latency budgets, token consumption ceilings, and cost-control
  thresholds (Gemini Context Caching, semantic routers to bypass LLM
  calls for repeated inquiries).

――――――――――――――――――――――――――――――――――――――――――――――――――

**5. Submission Checklist & Deliverables**

Your final repository/document should include:

4.  \`README.md\` containing your high-level architecture narrative and
    instructions to execute your agent code.

5.  An end-to-end **solution diagram image** (e.g., PNG, JPG, or a
    draw.io/Excalidraw schema) mapped in high-fidelity showing all cloud
    infrastructure decisions, telemetry flow segments, and secure
    perimeters.

6.  \`/proto\` directory containing your Python agent prototype code.

7.  \`/specialization\` directory containing your selected Track
    deep-dive specifications (including DDL schemas, Terraform
    definitions, or CI/CD workflow files depending on selection).

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><blockquote>
<p><strong>Note on Partial Submissions:</strong> We encourage you to
submit your response even if you cannot complete all sections or tasks.
Submit whatever functional code, architectural blocks, or track specific
deep-dives you have completed. We evaluate conceptual reasoning,
structural integration, and problem-solving velocity; partial
submissions with elegant, secure design blocks often score better than
fully completed generic or loose implementations. Our evaluation puts
significant weight on your architectural defense during the subsequent
live interview.</p>
</blockquote></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

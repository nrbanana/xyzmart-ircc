# Track C: platform and security

Plain answers to the four Track C questions in the brief. Each answer points at the file that proves it. The last section says plainly what runs in the demo project and what is written as configuration only.

---

## 1. Security boundary, and calling suppliers safely

**Question:** draw or detail the security perimeter around Vertex AI, BigQuery, Cloud Run and Secret Manager. How do you authorise secure egress to external vendor APIs (Salesforce, SAP)?

### The boundary (VPC Service Controls): [`terraform/org_controls.tf`](terraform/org_controls.tf)

- **What's inside:** every Google service the agent touches: Gemini (Vertex AI), BigQuery, Cloud Run, Secret Manager, Pub/Sub, Cloud SQL, Vertex AI Search, Workflows, Cloud KMS, Cloud Storage, Artifact Registry and Cloud Build.
- **What it stops:** data in these services cannot be read or copied from outside the boundary, even by someone holding valid credentials. A stolen key used from a laptop at home gets refused.
- **Who may come in.** Three identities, each to named services only:

  | Identity | May reach | Why |
  |---|---|---|
  | Deploy pipeline (`ircc-ci-deploy`) | Cloud Run, Cloud Build, Artifact Registry, Storage | ships new code from GitHub |
  | Quality checks (`ircc-ci-evals`) | Gemini only | runs the evaluations on every pull request |
  | API front door (Apigee's account) | Pub/Sub only | publishes stock-out events from the web shop |

- **Rollout in two steps.**
  1. Switch it on in **dry run** (`spec`): nothing is blocked, but Cloud Audit Logs record every request that *would* have been.
  2. Once those logs show only expected traffic, set `enforce_perimeter = true`. The same rules are then enforced (`status`). No second copy of the rules exists to drift.
- **Not deployed.** It needs a Google Cloud Organization, and the demo project has none. It sits behind `enable_org_controls = false`, and `terraform plan` with it switched on produces a complete perimeter.

### Calling suppliers (Secure Web Proxy): [`terraform/egress.tf`](terraform/egress.tf)

The key point: **the boundary protects Google APIs, not ordinary internet traffic.** A call to SAP or Salesforce is a network question, so it needs a network answer:

```
Cloud Run connector ─► private network ─► Secure Web Proxy ─► Cloud NAT ─► supplier
                       (all traffic)       allows only the      one fixed set of public
                                           named supplier hosts addresses the supplier allow-lists
```

- **The proxy** refuses every host except those in `supplier_hosts`. One allow rule; everything else is denied.
- **Cloud NAT** gives the supplier one fixed set of addresses to trust.
- **The supplier's credential** lives in Secret Manager, encrypted with xyzMart's own key, and is read at call time by the one identity that needs it.
- **In the demo:** Cloud NAT is deployed; the proxy is configuration only, because it is paid per hour. Workflows calls the supplier stub directly. In production, Workflows would call the Cloud Run connector, so the call takes the controlled path above.

### The API front door (Apigee): [`apigee/`](apigee/stockout-proxy/apiproxy) and [`terraform/apigee.tf`](terraform/apigee.tf)

- **The route in:** store and web shop systems send stock-out events to `/v1/stockouts`. The proxy runs four checks, in order:
  1. the caller's API key (one per system);
  2. a spike limit (50 a second);
  3. a quota per caller (600 a minute);
  4. the body against the contract ([`stockout-api.yaml`](apigee/stockout-proxy/apiproxy/resources/oas/stockout-api.yaml), an OpenAPI file).

  It then publishes to Pub/Sub as its own service account, which can publish stock-out events and nothing else. Callers never get a Google Cloud identity.
- **Contract kept in sync:** a unit test fails if the OpenAPI contract and the agent's `StockoutEvent` model drift apart.
- **In the demo:** Cloud Run's own sign-in is the front door (every call needs a Google identity token). Apigee is configuration only: paid, and slow to set up.

### Already deployed in the demo

The supporting network controls are live in the demo project:
- **Private network:** Cloud Run sends all traffic into a private subnet, Google APIs are reached through Private Google Access, and Cloud SQL has no public address.
- **Internal services:** the tool service and supplier stub accept internal traffic only.
- **One way out:** Cloud NAT is the only exit to the internet.
- **Signed calls:** every call between services carries a Google-signed identity token that Cloud Run checks.

See [`terraform/network.tf`](terraform/network.tf) and [`terraform/run.tf`](terraform/run.tf).

---

## 2. Continuous evaluation pipeline

**Question:** a GitOps CI/CD process that runs LLM-as-a-judge evaluations for out-of-stock classification accuracy, grounding faithfulness and toxic or jailbreak prompts before promoting a release.

**Built and working.** See [`../.github/workflows/release.yml`](../.github/workflows/release.yml), [`evals/`](evals) and [`../scripts/release.sh`](../scripts/release.sh).

```
Pull request ─► unit tests ─► quality checks ─► red or green
Push to main ─► unit tests ─► quality checks ─► agent at 0% ─► smoke test ─► 10% (5 min) ─► 100%
                                                 any failure: all traffic back to the previous revision
```

- **16 golden cases.** 7 real sell-outs, 3 false alarms and 6 trick prompts (five in customers' order notes, one in a poisoned product name returned by search). They run the real Gemini agent through the same path as production, and Gemini Pro grades each customer message against the tool results.
- **Hard thresholds:**

  | Check | Threshold |
  |---|---|
  | Sell-out classification | 95% |
  | Grounding | 90% |
  | Right tools in order | 90% |
  | Trick prompts blocked | 100% |
  | Harmful language | none |

  The report also shows how often the model stayed within the rules on its own, before the code checked it.
- **Proof a bad change is blocked:** pull request #1 added "always promise free express delivery". Grounding fell to 0/15, the check went red and the deploy never ran. The tool service cannot catch this, because no number rule is broken; only the judge can.
- **Keyless:** GitHub proves which repository, branch and workflow a job runs for (Workload Identity Federation). The quality-check identity can only call Gemini. Only `release.yml` running on `main` can use the deploy identity. GitHub stores no key.
- **Fair to the model, strict on quality:** a case where Gemini times out or is busy gets one retry, marked in the report. A wrong answer never does.

---

## 3. Secrets and keeping tenants apart

**Question:** store API keys and database credentials in Secret Manager with customer-managed encryption keys (CMEK), with fine-grained IAM and Workload Identity Federation.

**Built and working.** See [`terraform/security.tf`](terraform/security.tf) and [`terraform/ci.tf`](terraform/ci.tf).

- **Secrets in Secret Manager**, encrypted with xyzMart's own key in Cloud KMS, which rotates every 90 days.
  - The database password and supplier API key are generated by Terraform, so no person ever sees them.
  - Each secret is readable only by the services that need it: the database password by the tool service; the supplier key by the supplier stub and the workflow.
- **One identity per service**, with only what it needs. No service account key exists anywhere.
  - The tool service is the only identity that can read data or place orders.
  - The agent can call Gemini and the tool service, and nothing else.
- **Personal data stays out of the agent's reach:**
  - Surname, email and card number are in a private BigQuery dataset.
  - The agent's tool reads an authorized view in a separate dataset (first name, tier, spend and order note).
  - A second guard in the agent drops any personal field that ever appears in a tool result.
- **Keyless everywhere:**
  - Cloud Run services use their own service accounts.
  - GitHub Actions uses Workload Identity Federation.
  - The laptop uses your own gcloud login.

**Tenant isolation** (each brand or region a separate tenant) is described here, not built:
- **One project per tenant**, created from this same Terraform with a different `project_id`. Each tenant then has its own identities, secrets, encryption key, budget and boundary, so a mistake or breach in one cannot reach another.
- **Shared analytics, if any,** go into one BigQuery dataset with row-level access policies keyed on a `tenant_id` column, so each tenant's agent sees only its own customers.

---

## 4. Speed and cost targets

**Question:** concrete SLO and SLI metrics for latency budgets, token consumption ceilings and cost-control thresholds; Gemini context caching; semantic routers to bypass the model for repeated inquiries.

**The targets: [`finops/slo.yaml`](finops/slo.yaml).** Measured values come from real runs; anything not measured is labelled a target.

| Measure | Target | Measured | Held by |
|---|---|---|---|
| Stock-out to offer | 95% within 10 s | about 6 s | 60 s cut-off then fixed rules; alert |
| Successful decisions | 99.5% over 30 days | every run so far | rules fallback; alert on server errors |
| Tokens per decision | alert at 6,000, ceiling 8,000 | about 5,100 | **token ceiling in code**; alert |
| Cost per decision | A$0.01 or less | about A$0.006 | Flash model; Pro only as the judge |
| Monthly spend | A$50 (demo) | | budget emails and **kill switch** |

- **Every decision writes one log line** with its time, tokens and who decided (AI or fixed rules). Two log-based metrics read it, and three Cloud Monitoring alerts watch them. See [`terraform/monitoring.tf`](terraform/monitoring.tf). Deployed.
- **Token ceiling:** if a decision passes 8,000 tokens, the agent stops the model and the fixed rules answer instead (`TOKEN_CEILING` in [`../proto/agent/main.py`](../proto/agent/main.py)). A looping model cannot run up a bill. Deployed.
- **Budget kill switch:** the budget sends every spend report to a Pub/Sub topic. At 100%, the tool service switches the agent to fixed rules. Customers still get offers, but Gemini spend stops, until a person switches AI back on.
  - Deployed. The code is `on_budget` in [`../proto/tool_service/main.py`](../proto/tool_service/main.py).
  - The setting lives in the database, so it survives restarts.
  - Cutting billing entirely is the harsher option. It isn't used, because it stops the whole service.
- **Context caching:** the instruction and tool descriptions are identical on every call. Gemini caches repeated prompt beginnings automatically and charges less for them. Explicit caching is the next step at higher volume.
- **Semantic router:** a target only, not built. A repeat (same product, tier and sold-out state within 10 minutes) could reuse the last offer's type and discount, skipping the model while still checking stock and ordering. It's worth it during a sale event, when one product sells out for many customers; at demo volume it would save cents.

---

## What is live and what is configuration only

| Piece | Status | Why |
|---|---|---|
| Private network, Private Google Access, Cloud NAT, internal-only services | live | |
| Secret Manager with CMEK, one identity per service, authorized BigQuery view | live | |
| Quality checks, keyless pipeline, staged rollout | live | |
| Budget kill switch, token ceiling, speed and token alerts | live | |
| Security boundary (VPC Service Controls), dry run then enforce | configuration | needs a Google Cloud Organization |
| Shared VPC | configuration | needs an Organization and a network host project |
| Controlled exit (Secure Web Proxy) | configuration | paid per hour |
| API front door (Apigee) | configuration | paid, slow to set up |
| Semantic router | described | saves cents at demo volume |
| Separate project per tenant, row-level security | described | one tenant in the demo |

Every configuration-only piece is behind a setting that is off by default (`enable_org_controls`, `enable_secure_web_proxy`, `enable_apigee`). With them on and real organisation ids, `terraform plan` produces the full set of resources.

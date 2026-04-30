# DESIGN.md

## 📌 Project Overview

This project aims to build an **email-driven job application tracking and analytics system**.

The core idea:
> Automatically parse a user’s email inbox to extract job application activity and present it as a structured pipeline with actionable insights.

The system transforms:
- Unstructured email data → structured application records → analytics dashboard

---

## 🎯 Core Features

### Extraction
- Detect whether an email is job application-related
- Extract:
  - Company name
  - Application stage (applied, OA, interview, offer, etc.)
  - Timestamp
  - (future) application source (LinkedIn, Workday, etc.)

### Aggregation
- Group emails into applications
- Track progression over time

### Visualization
- Dashboard showing:
  - Active applications
  - Stage distribution
  - Timeline of updates

---

## 🧱 System Architecture (High-Level)

1. **Ingestion Layer**
   - Connect to email provider (e.g., Gmail API)
   - Fetch emails within a user-defined time range
   - Track processed email IDs to avoid duplication

2. **Filtering Layer**
   - Classify emails as relevant vs non-relevant
   - Hybrid approach:
     - Rule-based (keywords, sender domains)
     - Optional lightweight ML model (future)

3. **Extraction Layer**
   - Extract structured data from relevant emails
   - Hybrid approach:
     - Rules (regex, domain parsing)
     - LLM fallback for ambiguous cases

4. **Application Layer**
   - Group emails into application entities
   - Maintain current stage and timeline

5. **Storage Layer**
   - Store:
     - Email metadata (ID, timestamp, thread)
     - Extracted fields
     - Application records

6. **Frontend**
   - Dashboard UI for visualization and interaction

---

## 🚀 Development Phases

---

## Phase 1: Personal Project (Proof of Concept)

### Goal
Validate that email parsing → structured application tracking is feasible and useful.

### Scope
- Single user (self)
- Local or minimal backend
- No authentication required

### Features
- Manually fetch emails (via API or export)
- Basic filtering (keyword-based)
- Basic extraction (local ollama LLM)
- Simple output:
  - Table or JSON
  - Optional basic visualization

### Design Decisions
- Prioritize speed over perfection
- Use LLM freely if needed (low scale)
- Focus on correctness of pipeline

### Success Criteria
- Can correctly identify job-related emails
- Can extract company, role, and stage with reasonable accuracy
- Can group into applications

---

## Phase 2: MVP (Public Web App)

### Goal
Build a usable product for early users.

### Scope
- Multi-user system
- Hosted backend + frontend
- Basic authentication

### Features

#### Onboarding
- User connects email account
- Selects time range (e.g., last 3–6 months)

#### Core Functionality
- Incremental email scanning
- Deduplication (via message IDs + timestamps)
- Application grouping
- Dashboard:
  - Application list
  - Stage breakdown
  - Recent updates

#### Cost Optimization
- Hybrid pipeline:
  - Rule-based filtering first
  - LLM only for extraction when necessary
- Limit free usage (e.g., 3 months scan)

#### Monetization (Early)
- Freemium model:
  - Free: limited scan window
  - Paid: deeper history

### Design Decisions
- Separate ingestion from user-defined grouping
- Prioritize trust:
  - Do not scan attachments
  - Minimize stored data
- Optimize for low compute cost

### Success Criteria
- Users can onboard and see value quickly
- Pipeline is stable and reasonably accurate
- Costs remain low per user

---

## Phase 3: Early Commercialization

### Goal
Introduce sustainable monetization and improve retention.

### Features

#### Pricing Models
- Credits-based:
  - 1 credit = 1 email processed
  - Free initial credits
- Subscription:
  - Monthly plan (e.g., unlimited scanning within fair use)

#### Product Enhancements
- Improved extraction accuracy
- Incremental sync (auto-fetch new emails)
- Error correction (user edits)

#### Analytics
- Conversion funnel:
  - Applied → Interview → Offer
- Time between stages
- Application velocity

### Design Decisions
- Support both credits and subscription models
- Ensure fairness (no double charging for previously scanned emails)
- Track processed ranges and IDs

### Success Criteria
- Positive unit economics (cost < revenue per user)
- Users return to the product during job search

---

## Phase 4: Advanced Features & Scaling

### Goal
Differentiate product and increase value.

### Features

#### Advanced Analytics
- Application source detection:
  - LinkedIn, Workday, Handshake, etc.
- Performance by source:
  - Response rates
  - Interview conversion

#### Insights
- Follow-up suggestions
- Stale application detection
- Strategy recommendations

#### Data Layer
- Aggregate anonymized trends (future)
- Benchmarking across users (optional, privacy-sensitive)

### Infrastructure
- Scalable backend (e.g., AWS, autoscaling)
- Background job processing (queues)
- Efficient batch processing

### Design Decisions
- Treat uncertain data probabilistically
- Allow user correction to improve accuracy
- Maintain strict privacy guarantees

### Success Criteria
- High user retention
- Strong perceived value
- Clear differentiation from simple trackers

---

## 🔒 Privacy & Trust

Key principles:
- Only process email metadata and body text
- Do NOT process attachments
- Minimize stored raw email content
- Be transparent about data usage
- Allow users to revoke access and delete data

---

## 💰 Cost Strategy

- Avoid unnecessary LLM calls
- Use rule-based filtering aggressively
- Use lightweight models when possible
- Track:
  - Cost per email
  - Cost per user

Target:
> Maintain low per-user cost to support freemium and low-cost subscriptions

---

## 🧠 Key Design Principles

- **Start simple, iterate fast**
- **Accuracy > coverage**
- **User trust is critical**
- **Cost scales with usage → must be controlled**
- **Data structure enables analytics → design schema carefully**

---

## 📈 Long-Term Vision

Evolve from:
> Email parser

To:
> **Job search intelligence platform**

Where users can:
- Track applications automatically
- Understand performance
- Optimize their job search strategy

## Next Steps
- dynamic row counter for table
- last run was $0.10 for 400 emails, $1 mark would be at around 4k emails and 4 thousandth email in my inbox is almost 11 months ago for me so not bad.
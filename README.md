# Airia MAC Automation — AlayaCare Client Intake Pipeline

Automates the creation of AlayaCare clients from My Aged Care (MAC) referral PDFs using the [Airia](https://airia.ai) AI pipeline platform.

## What it does

1. **Upload** a MAC referral PDF in Airia chat
2. **Extract** structured client data (name, DOB, Medicare, address, care needs) via Gemini multimodal AI
3. **Review** the extracted details in an approval step before anything is written
4. **Create** the client in AlayaCare UAT via 3 API calls:
   - `POST /clients` — creates the client record
   - `PUT /clients/{id}` — updates full demographics (DOB, phone, address, Medicare)
   - `POST /contacts` — creates the client's self-contact

## Files

| File | Purpose |
|------|---------|
| `airia_pipeline_definition.json` | Airia pipeline export — import this into your Airia project |
| `alayacare_create_client.py` | Standalone Python script (alternative to Airia, uses Claude API) |
| `requirements.txt` | Python dependencies for the standalone script |
| `.env.example` | Template for environment variables |

## Airia Setup

### 1. Prepare your AlayaCare credentials

Base64-encode your AlayaCare API username and password:

```powershell
$creds = "YOUR_USERNAME:YOUR_PASSWORD"
[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($creds))
```

### 2. Import the pipeline

Post `airia_pipeline_definition.json` to your Airia project via the import API:

```powershell
$body = Get-Content airia_pipeline_definition.json -Raw
Invoke-RestMethod -Uri "https://prodaus.api.airia.ai/v1/PipelineImport/definition" `
  -Method POST -ContentType "application/json" `
  -Headers @{"X-API-Key" = "YOUR_AIRIA_API_KEY"} `
  -Body $body
```

### 3. Update the tool credentials

After import, open each of the three AlayaCare tools in the Airia pipeline editor and replace the `Authorization` header value:

```
Basic <YOUR_BASE64_ENCODED_ALAYACARE_CREDENTIALS>
```

with your actual Base64-encoded credentials from step 1.

### 4. Run the pipeline

Open the pipeline in Airia, upload a MAC referral PDF, and follow the approval prompt.

---

## Python Script Setup (alternative)

The standalone script uses Claude (Anthropic API) for PDF extraction and calls AlayaCare directly.

```powershell
# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
Copy-Item .env.example .env
# Edit .env with your AlayaCare and Anthropic credentials

# Run (dry run — no API calls)
python alayacare_create_client.py "path\to\MAC_referral.pdf" --dry-run

# Run for real
python alayacare_create_client.py "path\to\MAC_referral.pdf"
```

## AlayaCare → MAC Field Mapping

| MAC PDF field | AlayaCare field |
|---------------|----------------|
| Aged Care ID: AC… | `external_id` |
| Gender: Male/Female | `demographics.gender` (M/F) |
| Date of Birth DD/MM/YYYY | `birthday` YYYY-MM-DD |
| State SA in address | `timezone` Australia/Adelaide |
| Preferred correspondence: Post | `correspondence_method` mail |
| Phone – Mobile | `phone_personal` |
| Phone – Home | `phone_main` |
| Medicare number | `health_card` |
| Assessment recommended services | `care_needs` (summarised) |

## Environment

- **AlayaCare UAT:** `https://asa.uat.alayacare.com`
- **Airia tenant:** `https://prodaus.airia.ai`
- **AI model:** Gemini 3.1 Flash Lite (multimodal, for PDF reading)

# InvoiceIQ — Installation Guide

**Version:** 1.0.0  
**Platform:** Windows 10 (1809) or later — 64-bit  
**Part of the SmartIQ product family**

---

## Overview

InvoiceIQ automates accounts payable for Sage 50 UK. It monitors an O365 mailbox for invoice attachments, extracts data using AI, matches against purchase orders, posts to Sage 50, and routes invoices above a threshold for approval.

There are two installation paths:

| Path | Who it's for |
|---|---|
| **Installer (recommended)** | End users — runs the built `.exe`, no Python needed |
| **Developer setup** | Developers — runs directly from source with Python |

---

## Path A — End User Install (Installer)

### Prerequisites
- Windows 10 version 1809 or later (64-bit)
- Sage 50 Accounts (UK) installed on the same machine
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- A Microsoft 365 mailbox configured for invoice receipt
- An Azure AD app registration with `Mail.Read` and `Mail.ReadWrite` permissions

### Step 1 — Run the installer
1. Double-click **InvoiceIQ-Setup-1.0.0.exe**
2. Accept the licence and choose an install folder (default: `C:\Program Files\InvoiceIQ`)
3. Optionally tick **Create desktop shortcut** and/or **Start InvoiceIQ when Windows starts**
4. Click **Install**, then **Finish**

### Step 2 — First launch
1. Double-click the **InvoiceIQ** desktop icon (or Start Menu shortcut)
2. The setup wizard opens in your default browser at `http://localhost:5000`
3. Create your first user account when prompted

### Step 3 — Configure the application
Work through each section of the Settings page:

#### Microsoft 365 Email
| Field | Where to find it |
|---|---|
| Tenant ID | Azure Portal → Azure Active Directory → Overview |
| Client ID | Azure Portal → App registrations → your app → Overview |
| Client Secret | Azure Portal → App registrations → your app → Certificates & secrets |
| Mailbox | The email address InvoiceIQ should monitor (e.g. `invoices@company.com`) |
| Polling interval | How often to check for new emails (default: 5 minutes) |
| Processed folder | Mailbox folder emails move to after processing (default: `AP Processed`) |

#### Sage 50
| Field | Description |
|---|---|
| Data path | Full path to the Sage 50 company data folder (e.g. `C:\ProgramData\Sage\Accounts\2024\Company.000`) |
| Username | Sage 50 username (usually `Manager`) |
| Password | Sage 50 password |
| SDO version | Leave as `300` unless you are on an older Sage version |

#### Claude AI
| Field | Description |
|---|---|
| API key | Your Anthropic API key from [console.anthropic.com](https://console.anthropic.com) |

#### Approval Workflow
| Field | Description |
|---|---|
| Enable approvals | Tick to route invoices above the threshold to an approver |
| Threshold amount | Invoices at or above this value (GBP) require approval before posting |
| Approvers | Add the names and email addresses of your approvers |

### Step 4 — Start monitoring
Click **Start Email Monitor** on the dashboard. InvoiceIQ will begin polling the mailbox on the configured interval.

---

## Path B — Developer Setup

### Prerequisites
- Windows 10 (1809) or later — 64-bit
- **Python 3.12** ([python.org](https://python.org/downloads)) — tick "Add Python to PATH" during install
- Sage 50 Accounts (UK) installed on the same machine (for the Sage connector)
- Git (optional, for cloning)

### Step 1 — Install Python dependencies

Open a command prompt in the project folder and run:

```bat
setup.bat
```

Or manually:

```bat
pip install -r requirements.txt
```

> **Note:** `pywin32` requires a post-install step on some systems:
> ```bat
> python -m pywin32_postinstall -install
> ```

### Step 2 — Configure settings

Copy the default settings file and fill in your values:

```bat
copy config\settings.default.json config\settings.json
```

Open `config\settings.json` and fill in:

```json
{
  "email": {
    "tenant_id":    "YOUR-AZURE-TENANT-ID",
    "client_id":    "YOUR-AZURE-CLIENT-ID",
    "client_secret":"YOUR-AZURE-CLIENT-SECRET",
    "mailbox":      "invoices@yourcompany.com",
    "polling_interval_minutes": 5,
    "processed_folder": "AP Processed"
  },
  "sage": {
    "enabled":   true,
    "data_path": "C:\\ProgramData\\Sage\\Accounts\\2024\\Company.000",
    "username":  "Manager",
    "password":  "your-sage-password",
    "sdo_version": "300"
  },
  "claude": {
    "api_key": "sk-ant-...",
    "model":   "claude-opus-4-7"
  },
  "approval": {
    "enabled":          true,
    "threshold_amount": 1000.00,
    "approvers":        []
  },
  "app": {
    "company_name": "Your Company Ltd",
    "currency":     "GBP",
    "port":         5000
  }
}
```

### Step 3 — Run the application

```bat
python run.py
```

The dashboard opens automatically at `http://localhost:5000`.

---

## Building the Installer (Developers)

To produce the distributable installer:

### Prerequisites
- [PyInstaller](https://pyinstaller.org) — included in `requirements.txt`
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) — free installer compiler

```bat
rem Build the one-dir executable
python build.py

rem Then compile the installer
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\setup.iss
```

Output: `dist\InvoiceIQ-Setup-1.0.0.exe`

---

## Microsoft 365 — App Registration Guide

1. Go to [portal.azure.com](https://portal.azure.com) and sign in as a Global Admin
2. Navigate to **Azure Active Directory → App registrations → New registration**
3. Name: `InvoiceIQ`, Supported account types: **Single tenant**
4. Click **Register**
5. Under **API permissions → Add a permission → Microsoft Graph → Application permissions**, add:
   - `Mail.Read`
   - `Mail.ReadWrite`
6. Click **Grant admin consent**
7. Under **Certificates & secrets → New client secret**, create a secret and copy the value immediately

---

## Data & File Locations

| Item | Default location |
|---|---|
| Database | `<install folder>\data\invoiceiq.db` |
| Configuration | `<install folder>\config\settings.json` |
| Stored invoice PDFs | `<install folder>\invoices\` |
| Logs | `<install folder>\logs\` |

---

## Uninstalling

Use **Add or Remove Programs → InvoiceIQ → Uninstall**.  
You will be asked whether to keep or delete your data files (database, invoices, configuration).

---

## Support & Licensing

**Product:** InvoiceIQ  
**Brand:** SmartIQ  
**Contact:** [your-support-email]

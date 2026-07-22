# Photostore

An open-source, self-hostable photo gallery you can deploy into your own
Azure subscription — with ratings, likes, tagging, face/people clustering,
semantic search, and duplicate detection.

Licensed under [AGPL-3.0](LICENSE).

## Deploy your own (one click, no coding)

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2FGit4Rajat%2Fphotostore%2Fmain%2Fdeploy%2Fazuredeploy.json/createUIDefinitionUri/https%3A%2F%2Fraw.githubusercontent.com%2FGit4Rajat%2Fphotostore%2Fmain%2Fdeploy%2FcreateUiDefinition.json)

This provisions the whole app into your own Azure subscription from prebuilt
public container images — no build step required. The template lives at
[`deploy/`](deploy/) (`main.bicep` → compiled `azuredeploy.json`).

The template deploys at **subscription scope** and **creates its own resource
group** (`<appName>-rg` by default), so you're only asked for a subscription,
a region, and a few details — no need to pick or create a resource group first.

### Sign-in — just set an email and password

In the deploy form you set a **login email** and **password**. That's your
sign-in — no Microsoft account, app registrations, or admin consent required.
When the deployment finishes, open the app URL and log in.

- **Forgot your password?** Use "Forgot password?" on the login screen. A reset
  link is emailed to you via Azure Communication Services (provisioned by the
  template with an Azure-managed sender domain — no DNS setup). Reset emails may
  land in your spam folder.
- **Break-glass reset:** you can always reset by updating the `owner-password`
  secret on the `<appName>-backend` Container App in the Azure Portal.
- **Change your password** anytime from inside the app.

> **Advanced: Microsoft Entra SSO.** Prefer enterprise single sign-on instead of
> a password? Deploy, then run [`deploy/setup-auth.sh`](deploy/setup-auth.sh) in
> [Azure Cloud Shell](https://portal.azure.com) to create Entra app
> registrations and switch the app to `AUTH_MODE=entra`. This requires rights to
> register apps and grant admin consent in your directory.

> **Note:** the button pulls the public images
> `ghcr.io/git4rajat/photostore-backend:latest` and `-frontend:latest`. These
> must be published (via the [Publish images workflow](.github/workflows/publish-images.yml))
> and set to **Public** in GHCR before a deploy can succeed.

## Repository Layout

- [`frontend/`](frontend/) — React + TypeScript + Vite frontend
- [`backend/`](backend/) — Flask backend (containerized API + worker)
- [`deploy/`](deploy/) — Azure ARM/Bicep one-click deployment template

## Features

### Photo Management
- Upload, organize, and manage photos
- Automatic thumbnail generation
- Batch delete operations
- Search by filename

### Metadata & Engagement
- ⭐ **Rate photos** (1-5 stars)
- ❤️ **Like/unlike photos** with count tracking
- 🏷️ **Tag photos** with custom labels
- 📍 **Add location metadata** (latitude, longitude, address)

### Smart Filtering
- Filter by minimum rating
- Filter by minimum likes
- Filter by location (geospatial)
- Combine multiple filters

### Duplicate Detection
- **Exact duplicates**: SHA256 hash comparison
- **Similar images**: Perceptual hashing (pixel-level comparison)
- Automatic warnings on upload

## Architecture

### Tech Stack
- **Frontend**: React + TypeScript + Vite
- **Backend**: Flask + Python
- **Storage**: Azure Blob Storage (images) + Azure Table Storage (metadata)
- **Auth**: Microsoft Entra ID (Azure AD)
- **Deployment**: Azure Container Apps

### Storage Strategy
- **Photos**: Azure Blob Storage (`images` container)
- **Thumbnails**: Azure Blob Storage (`thumbnails` container)
- **Metadata**: Azure Table Storage (`photometadata` table)
  - Per-user organization (partition by user ID)
  - Indexed queries for fast filtering
  - Extremely cost-effective (~$0.01 per 100K transactions)

## Local Development

> **Prerequisite:** this repo stores the face-recognition model (`*.onnx`) via
> [Git LFS](https://git-lfs.com). Install it before cloning
> (`git lfs install`) so the model is pulled, not left as a pointer file. If
> you already cloned without it, run `git lfs pull`.

### Backend Setup
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your values (storage account,
allowed origins, optional auth), then run the Flask backend locally:
```bash
cp .env.example .env
flask run --port 5001
```

### Frontend Setup
```bash
cd frontend
npm install
cp .env.example .env   # point the API base URL at your backend
npm run dev
```

## API Endpoints

### Photos
- `GET /photos` - List photos with pagination
- `POST /upload/init` + `POST /upload/finalize` - Direct-to-blob upload flow (with duplicate detection)
- `POST /photos/delete` - Batch delete photos

### Metadata
- `POST /photos/{filename}/rating` - Rate a photo (1-5)
- `POST /photos/{filename}/like` - Like/unlike a photo
- `GET /photos/{filename}/metadata` - Get full metadata

### Filtering
- `GET /photos/filter` - Filter by rating, likes, location

## Environment Variables

Backend and frontend configuration is documented in their respective
`.env.example` files:

- Backend: [`backend/.env.example`](backend/.env.example)
- Frontend: [`frontend/.env.example`](frontend/.env.example)

Key backend variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `STORAGE_ACCOUNT_NAME` | Storage account name used with managed identity (DefaultAzureCredential) | Required |
| `ALLOWED_ORIGINS` | Comma-separated allowed frontend origins | Required |
| `BLOB_IMAGE_CONTAINER` | Blob container for full images | `images` |
| `BLOB_THUMBNAIL_CONTAINER` | Blob container for thumbnails | `thumbnails` |
| `METADATA_TABLE` | Table Storage name for metadata | `photometadata` |
| `AUTH_REQUIRED` | Require authentication for API access | `false` |

## Cost Estimate

For **500,000 photos**:
- Blob Storage: ~$7.50/month
- Table Storage: ~$5/month
- **Total**: ~$12.50/month (excluding Container Apps compute)

## Security

- SAS tokens for blob access (short expiry)
- Azure Entra ID authentication (optional)
- Server-side validation of all operations
- Secure filename handling

## Limitations

- Table Storage max entity size: 1MB (metadata only)
- Query performance depends on partition key distribution
- Perceptual hashing ~50-150ms per image

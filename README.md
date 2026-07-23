# Photostore

Photostore is your own private photo library — a place to upload, browse, and
rediscover your photos without handing them to a big-tech cloud. It runs
entirely in *your* Azure subscription, so your memories stay yours.

Once your photos are in, Photostore does the tedious work for you: it makes
fast-loading previews, reads text and locations off your pictures, tags what's
in them, recognizes the people who show up again and again, and lets you search
your whole library in plain language. You organize the rest into albums — by
hand or automatically — and share them with a link.

It's open source and self-hostable, licensed under [AGPL-3.0](LICENSE).

## What you can do

- **Browse & upload** your whole library in a fast, responsive gallery, with
  automatic thumbnails and support for both photos and videos.
- **Search in plain language** — type *"beach at sunset"* or *"birthday cake"*
  and Photostore finds matching photos, not just filename matches.
- **Rate, like, and tag** photos, and filter the gallery down to exactly what
  you want (minimum rating, likes, media type, and more).
- **Organize into albums** — build them by hand or let Photostore create *smart
  albums* automatically by place, date, person, or what's in the picture.
- **Find your people** — Photostore groups faces into people you can name, so
  you can pull up everyone of a given person in a tap.
- **Share albums** with a public link (optionally protected by an access code),
  and let visitors download the photos.
- **Stay tidy** — Photostore flags exact and near-duplicate uploads and surfaces
  corrupted files so your library stays clean.

Much of the heavy lifting (thumbnails, text extraction, tagging, face
detection) happens right in your browser, so your photos aren't shipped off to
a third-party AI service to be understood.

## The main pages

### 📸 Gallery — your whole library

The gallery is the home for every photo and video you've uploaded. From here you
can:

- **Upload** new photos and videos, with automatic thumbnail generation and
  duplicate warnings if a file already exists (exact or visually similar).
- **Search your library in natural language** — semantic search understands what
  a photo *is* of, so "*dog in the snow*" works even when nothing is named that.
- **Filter** by minimum rating, minimum likes, and media type (all / photos /
  videos), and combine filters to narrow things down.
- **Rate ⭐, like ❤️, and tag 🏷️** photos as you go.
- **Select photos** to download in bulk or turn straight into a new album.

### 🗂️ Albums — organize and share

Albums are collections you curate on top of your library. On the Albums page you
can:

- **Create albums by hand** — name an album and add any photos you've selected.
- **Create smart albums automatically**, where Photostore fills the album for you
  from a rule:
  - **By Location** — places across your library
  - **By Recent Upload** — your latest upload window
  - **By Person** — matched people from face clustering
  - **By Event/Time** — a capture-date window
  - **By Tag/Object** — AI tags and detected objects
- **Search and filter within an album** (by name, minimum rating, liked-only) to
  find a specific shot fast.
- **Share an album as a public link** — optionally locked behind an access code —
  so anyone with the link can view and download the photos, read-only.
- **Download** an entire album (or a selection) as a batch.

### 🧰 Tools — see how your photos were processed

The Tools page is the behind-the-scenes view of everything Photostore does to
each photo after upload. Every photo runs through a set of processing stages:

- **Thumbnails** — browser-created previews
- **EXIF** — capture date and GPS metadata
- **OCR** — text extracted from the image, in your browser
- **AI vision** — tags and captions generated in your browser
- **Map tagging** — reverse-geocoding GPS into place names
- **Face detection** — detecting and clustering faces, in your browser

For each photo you can see the status of every stage at a glance, and **filter**
to find work that still needs doing — e.g. photos that *failed* a stage or that
have *no data* for a particular process (thumbnail, EXIF, OCR, AI vision, map,
or face). It's where you go to check that processing is complete or to chase
down anything that got stuck. Related: the **Corrupted uploads** view surfaces
files that couldn't be processed at all.

### 👥 People — faces, grouped and named

Photostore detects faces and clusters photos of the same person together. On the
People page you can:

- **Browse the people** it has found, each as a cluster of face crops.
- **Name a person** so you can recognize and search for them.
- **Search** across your people, and **assign unclustered faces** that haven't
  been grouped yet.
- **Merge clusters** that are actually the same person (with an undo), and
  **split** faces that were grouped by mistake into their own cluster.
- **Confirm or reject** individual faces — mark a low-confidence detection as
  correct, or flag something that isn't really a face.

All face detection and clustering runs in your browser, so faces are recognized
without sending your photos to an outside service.

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
- **Your email is your sign-in identity.** Set the login **email** (`OWNER_EMAIL`)
  in the deploy form — password-mode sign-in is by email + password, so an empty
  email leaves the owner unable to log in. If you deployed without one, set
  `OWNER_EMAIL` on the `<appName>-backend` Container App and restart; the account
  reconciles to that address on the next boot.

### Sharing your library

From the **Sharing** tab you can invite other people by email. Choose whether
they **join your library** (they see the same photos, as an equal member — up to
15 people including you) or **start their own** fresh library. Invitees get an
email link (valid 72 hours) to set a password and sign in. Only the library
owner can invite or remove members; any member can leave, and switching between
libraries you belong to happens from the same tab. (Sharing requires the reset
email transport above to be configured, since invites are delivered by email.)

> **Advanced: Microsoft Entra SSO.** Prefer enterprise single sign-on instead of
> a password? Deploy, then run [`deploy/setup-auth.sh`](deploy/setup-auth.sh) in
> [Azure Cloud Shell](https://portal.azure.com) to create Entra app
> registrations and switch the app to `AUTH_MODE=entra`. This requires rights to
> register apps and grant admin consent in your directory.

> **Note:** the button pulls the public images
> `ghcr.io/git4rajat/photostore-backend:latest` and `-frontend:latest`. These
> must be published (via the [Publish images workflow](.github/workflows/publish-images.yml))
> and set to **Public** in GHCR before a deploy can succeed.

## How to use the app

This is a quick, task-by-task guide to getting the most out of Photostore once
you're signed in.

### Uploading photos

1. Go to the **Gallery** and click **Upload**.
2. Pick one or more photos or videos (you can drag them in).
3. Photostore uploads each file and immediately starts processing it in the
   background — you'll see thumbnails appear as they're ready.
4. If a file is an **exact or near-duplicate** of something already in your
   library, you'll get a warning before it's added, so you can skip it.

Processing (thumbnails, text, tags, faces, location) keeps running after upload —
you don't have to wait on it. Check progress any time on the **Tools** page.

### What the icons mean

You'll see the same icons throughout the gallery and on individual photos:

- **⭐ Stars — rating.** Click a star to rate a photo from 1 to 5. Click the same
  star again to clear the rating.
- **❤️ Heart — like.** Click to like a photo; click again to unlike. Likes are a
  quick "favorite" flag, separate from the star rating.
- **🏷️ Tag — tags.** Shows the tags on a photo. AI-generated tags appear
  automatically; you can also add your own.
- **⬇️ Download** — save the photo (or your current selection) to your device.
- **🗑️ Trash — delete** the photo from your library.
- **☑️ Select** — tick photos to act on several at once (bulk download, delete,
  or "make an album from these").

### Filtering the gallery

Use the filter controls at the top of the Gallery to narrow things down:

- **Minimum rating** — show only photos at or above a star level.
- **Liked only** — show only photos you've hearted.
- **Media type** — all, photos only, or videos only.
- **Search** — type in plain language (*"beach at sunset"*, *"birthday cake"*)
  and Photostore finds matching photos by what's *in* them, not just filenames.

Filters combine, so you can, for example, show only liked videos rated 4+.

### Working with People

The **People** page groups faces that belong to the same person into a cluster
of face crops. From here you can:

- **Name a person** — click a cluster and give it a name. Once named, you can
  search for that person and use them in smart albums.
- **Assign unclustered faces** — faces that weren't confidently grouped show up
  separately; assign them to the right person.
- **Merge clusters** — if two clusters are actually the same person, merge them
  (there's an **undo** if you merge by mistake).
- **Split a cluster** — if faces were grouped together wrongly, split the odd
  ones out into their own cluster.
- **Confirm or reject faces** — confirm a low-confidence face as correct, or
  reject something that isn't really a face.
- **Search people** by name to jump straight to someone.

### Using the Tools page (and re-running AI actions)

The **Tools** page shows every processing stage each photo goes through:

- **Thumbnails** — fast-loading previews
- **EXIF** — capture date and GPS location
- **OCR** — text read out of the image
- **AI vision** — tags and captions describing the photo
- **Map tagging** — turning GPS into a place name
- **Face detection** — finding and grouping faces

For each photo you can see the status of every stage at a glance. Use the
**filters** to find work that still needs doing — for example, photos that
**failed** a stage or have **no data** for a given process.

**Re-running a stage:** if a photo failed or gave a bad result (a missing tag, a
face that wasn't detected, wrong location), filter to the affected photos and
**re-run** that processing stage on them. This kicks off the AI action again for
those photos without touching anything else. The **Corrupted uploads** view
lists files that couldn't be processed at all.

### Working with Albums

Albums are collections you build on top of your library.

**Create an album by hand:**

1. In the **Gallery**, select the photos you want (☑️).
2. Choose **Create album** (or "make an album from selection").
3. Give it a name — your new album now holds those photos.

**Create a smart album** (Photostore fills it automatically from a rule) — on the
**Albums** page choose **New smart album** and pick a rule:

- **By Location** — a place from your library
- **By Recent Upload** — your latest upload window
- **By Person** — a named person from the People page
- **By Event/Time** — a capture-date window
- **By Tag/Object** — an AI tag or detected object

**Add or remove photos in an existing album:**

- **To add:** open the album (or select photos in the Gallery) and use **Add to
  album** to drop the selected photos in.
- **To remove:** open the album, select the photos you want out, and choose
  **Remove from album**. Removing a photo from an album does **not** delete it
  from your library — it only leaves that album.

**Search within an album** by name, minimum rating, or liked-only to find a
specific shot fast, and **download** the whole album (or a selection) as a batch.

### Sharing an album

1. Open the album you want to share.
2. Choose **Share** to create a **public link**.
3. Optionally set an **access code** so only people with the code can open it.
4. Send the link. Visitors get a **read-only** view — they can browse and
   **download** the photos, but can't change anything.

To stop sharing, open the album's share settings and revoke the link.

### Changing your password

You can change your password any time from inside the app (see your account
settings). Forgot it? Use **Forgot password?** on the login screen to get a
reset link by email — check your spam folder if it doesn't arrive.

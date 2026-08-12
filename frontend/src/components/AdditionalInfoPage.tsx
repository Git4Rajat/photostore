import React, { useEffect, useState } from 'react';
import { get } from '../services/apiClient';
import { getRuntimeConfig } from '../config/appConfig';

const AdditionalInfoPage: React.FC = () => {
    const [throughput, setThroughput] = useState<{ uploads?: { mbPerSecond?: number }; processed?: { mbPerSecond?: number } }>({});
    const buildTs = getRuntimeConfig().buildTimestamp || '';
    const buildLabel = buildTs ? `Built: ${new Date(buildTs).toLocaleString()}` : 'Build: unknown';

    useEffect(() => {
        let mounted = true;
        void (async () => {
            try {
                const data = await get('/api/performance/throughput');
                if (mounted) {
                    setThroughput(data || {});
                }
            } catch {
                if (mounted) {
                    setThroughput({});
                }
            }
        })();
        return () => {
            mounted = false;
        };
    }, []);

    const uploadsMbPerSecond = Number(throughput?.uploads?.mbPerSecond || 0).toFixed(2);
    const processedMbPerSecond = Number(throughput?.processed?.mbPerSecond || 0).toFixed(2);
    return (
        <section className="card-glass additional-info-wrap">
            <header className="additional-hero">
                <p className="additional-kicker">OPEN SOURCE AND SELF HOSTED</p>
                <h2 className="additional-title">Keepsake Capabilities</h2>
                <p className="additional-subtitle">
                    A self-hosted photo backup and discovery workspace built on Azure Container Apps and Azure Storage.
                </p>
            </header>

            <div className="additional-grid">
                <article className="additional-card">
                    <h3>What It Does Today</h3>
                    <p>
                        Keepsake keeps original photos in your own Azure Storage account, generates thumbnails, extracts
                        metadata, and gives the gallery fast ways to browse, search, organize, and recover files later.
                    </p>
                    <p>
                        The app is designed for private or family-style archives where the storage remains under the
                        owner&apos;s Azure subscription instead of a third-party photo service.
                    </p>
                </article>

                <article className="additional-card">
                    <h3>Upload Reliability</h3>
                    <ul>
                        <li>Direct-to-Blob uploads use the Azure Blob SDK so large files bypass the backend data path.</li>
                        <li>Uploads can continue while the user navigates inside the app and can resume after long pauses.</li>
                        <li>Adaptive chunking and parallel file uploads tune throughput for desktop, mobile, and slower networks.</li>
                        <li>SHA-256 verification catches incomplete or corrupted uploads before they are treated as healthy.</li>
                        <li>Exact duplicate checks compare file hashes during finalization.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Storage Model</h3>
                    <ul>
                        <li><strong>Originals</strong>: Azure Blob Storage image container.</li>
                        <li><strong>Thumbnails</strong>: separate Blob container for cheaper and faster gallery display.</li>
                        <li><strong>Metadata</strong>: Azure Table Storage, partitioned by user and keyed by filename.</li>
                        <li><strong>Queues</strong>: Azure Queue Storage drives thumbnail, AI vision, map, face, and clustering work.</li>
                        <li><strong>Compute</strong>: Container Apps can split frontend, backend, upload, worker, clustering, and search services.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Gallery and Discovery</h3>
                    <ul>
                        <li>Gallery browsing supports sorting, paging, capture-date filters, likes, ratings, and EXIF details.</li>
                        <li>AI search runs when the user submits the query, not on every keystroke.</li>
                        <li>Context-aware search treats queries like “red dress” as object plus modifier.</li>
                        <li>Search can combine tags, people, location, OCR text, captions, and semantic signals.</li>
                        <li>Recent uploads, map data, camera metadata, and processing state are visible from photo details.</li>
                        <li>HEIC and RAW (CR3) files decode in-browser for accurate previews, no native format support required.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>AI and Processing</h3>
                    <ul>
                        <li>Open-source AI vision suggests tags, objects, captions, OCR text, and semantic search text.</li>
                        <li>Map detection extracts GPS data and resolves readable location fields when possible.</li>
                        <li>Face detection stores face records for people clustering and manual review.</li>
                        <li>Processing status tracks thumbnails, vision, maps, and faces as queued, running, done, failed, or no data.</li>
                        <li>The Tools page can queue or retry processing work without re-uploading files.</li>
                        <li>In-app notifications (bell and toast) surface when a background processing job finishes.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>People and Faces</h3>
                    <ul>
                        <li>Detected faces are clustered into people you can name, search, and use in smart albums.</li>
                        <li>Merge clusters that belong to the same person, with undo if you merge by mistake.</li>
                        <li>Split a cluster if faces were grouped together incorrectly.</li>
                        <li>Confirm or reject individual low-confidence face detections.</li>
                        <li>Unclustered faces can be reviewed and assigned to a person manually.</li>
                        <li>Clustering aggressiveness is tunable (strictest, strict, balanced, loose) without a redeploy.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Organization</h3>
                    <ul>
                        <li>Albums collect selected photos by hand, or auto-fill as smart albums by location, date, person, or tag/object.</li>
                        <li>Corrupted uploads are separated into their own page with hash and verification context.</li>
                        <li>Download workflows can package selected photos or a whole album for export.</li>
                        <li>Processing counters and health views help find missing thumbnails, failed AI runs, and map/face gaps.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Sharing and Multi-User Libraries</h3>
                    <ul>
                        <li>Albums can be shared as public, read-only links, optionally protected with an access code.</li>
                        <li>Library owners can invite up to 15 members into a shared library with equal access.</li>
                        <li>Invited members can instead start their own separate library from the same invite flow.</li>
                        <li>Invite links are single-use and expire after 72 hours; members can leave a shared library at any time.</li>
                        <li>&quot;Clean library&quot; wipes content while keeping the account, and requires the owner plus a second member to confirm.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Security and Hosting</h3>
                    <ul>
                        <li>Sign-in is email and password by default, with optional Microsoft Entra SSO for enterprise use.</li>
                        <li>Backend metadata and blob access are scoped to the caller&apos;s active library on every request.</li>
                        <li>Blob upload SAS URLs are short-lived and created by the backend for the current upload.</li>
                        <li>The infrastructure direction is managed identity first, reducing dependency on storage keys.</li>
                        <li>The app is intended to run with near-zero idle cost by allowing services to scale to zero.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Current Limits and Roadmap</h3>
                    <ul>
                        <li>Exact duplicate detection is hash-based; similar-photo matching should stay background-only at scale.</li>
                        <li>Large gallery reads now use paged Table Storage scans, but a dedicated index will scale better for very large libraries.</li>
                        <li>Vector search can be made faster by precomputing embeddings and loading them into a dedicated search container.</li>
                        <li>Reclustering needs non-destructive safeguards so failed clustering runs do not hide previously assigned people.</li>
                        <li>Cold-start aware UI should explain when scaled-to-zero services are warming up.</li>
                    </ul>
                </article>
            </div>
            <footer className="additional-footer">
                <small>{`Uploads: ${uploadsMbPerSecond} MB/s`}</small>
                <small>{`Processed: ${processedMbPerSecond} MB/s`}</small>
                <small>{buildLabel}</small>
            </footer>
        </section>
    );
};

export default AdditionalInfoPage;

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
                <h2 className="additional-title">What Keepsake Does For You</h2>
                <p className="additional-subtitle">
                    A private photo library that keeps every original, organizes it automatically, and lets you find any
                    memory in seconds — on infrastructure you own, not a subscription photo service.
                </p>
            </header>

            <div className="additional-grid">
                <article className="additional-card">
                    <h3>Your Originals, Safe and Untouched</h3>
                    <ul>
                        <li>Every photo and video is stored as a permanent, unmodified original in storage you own.</li>
                        <li>Thumbnails, dates, camera details, and GPS locations are prepared on arrival, so new photos are ready to browse right away.</li>
                        <li>HEIC and Canon RAW (CR3) files preview directly in the browser — no conversion needed.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Uploads That Survive Interruptions</h3>
                    <ul>
                        <li>Upload large batches of photos, RAW files, and video from any device; transfers continue in the background.</li>
                        <li>A dropped connection, closed tab, or sleeping phone resumes where it left off — no starting over.</li>
                        <li>Speed adapts to your connection, and you can stop or discard an upload at any time.</li>
                        <li>Files are verified on arrival, and exact duplicates are rejected before they use any space.</li>
                        <li>Failed or corrupted uploads are set aside for review instead of silently disappearing.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Find Any Photo in Seconds</h3>
                    <ul>
                        <li>Search in plain language — “red dress,” “birthday cake,” “dog at the beach.”</li>
                        <li>Search uses everything Keepsake knows: tags, captions, text in the image, people, and place.</li>
                        <li><strong>Ask</strong> answers questions about your library, and <strong>Explore</strong> surfaces places, people, and moments to browse.</li>
                        <li>Filter by date, rating, likes, and media type; open any photo for its map, camera settings, and processing status.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Recognize the People You Love</h3>
                    <ul>
                        <li>Faces are grouped into people automatically — search a name to see every photo they&apos;re in.</li>
                        <li>Merge duplicate groups (with undo), split mixed-up ones, and confirm, reject, or manually assign individual faces.</li>
                        <li>Tune face matching to be stricter or looser; changes apply immediately.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Albums, Trash, and Tools</h3>
                    <ul>
                        <li>Build albums by hand, or use smart albums that fill themselves by place, date, person, or subject.</li>
                        <li>Download a selection or a whole album in one package.</li>
                        <li>Deleted photos and albums rest in Recently Deleted for 30 days; restore them any time before they&apos;re purged.</li>
                        <li>The Tools page shows what is missing (thumbnails, tags, locations, faces) and lets you repair or re-run it without re-uploading. A bell notifies you when background jobs finish.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Share With Family</h3>
                    <ul>
                        <li>Turn any album into a public, read-only link — no account needed — optionally locked with an access code.</li>
                        <li>Invite up to 15 members into one shared library; invitees can also choose to start their own library.</li>
                        <li>Invite links are single-use and expire after 72 hours; any member can leave at any time.</li>
                        <li>“Clean library” wipes content but keeps accounts, and requires the owner plus a second member to confirm.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Private and Low-Cost by Design</h3>
                    <ul>
                        <li>Sign in with email and password, or with your personal Microsoft account.</li>
                        <li>You only ever see photos in your own library — in the gallery, in search, and through shared links.</li>
                        <li>Links to originals are generated per request and expire quickly.</li>
                        <li>Background workers scale to zero when idle, so hosting costs stay near zero between visits.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Coming Next</h3>
                    <ul>
                        <li>Near-duplicate detection (similar shots, not just exact copies), run in the background without slowing uploads.</li>
                        <li>Faster “find similar photos” using precomputed embeddings.</li>
                        <li>Safeguards so a failed re-clustering run can never hide people you&apos;ve already named.</li>
                        <li>A clear “warming up” message in the UI when idle workers are waking.</li>
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

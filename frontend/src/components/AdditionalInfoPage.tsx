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
                    A private photo library that backs up every original, organizes it automatically, and helps you
                    find any memory in seconds — running entirely in storage you own, not a subscription photo service.
                </p>
            </header>

            <div className="additional-grid">
                <article className="additional-card">
                    <h3>Your Photos, Backed Up Automatically</h3>
                    <p>
                        Every photo and video you upload is kept as a permanent, untouched original in your own Azure
                        Storage account — not locked inside a subscription you could lose access to or get priced out of.
                    </p>
                    <p>
                        Keepsake also builds fast-loading thumbnails and pulls out useful details like date, camera, and
                        location as soon as a photo arrives, so your library is ready to browse right away. It&apos;s
                        built for private or family archives where you stay in control of where the photos actually live.
                    </p>
                </article>

                <article className="additional-card">
                    <h3>Upload From Anywhere, Without Worry</h3>
                    <ul>
                        <li>Upload straight from your phone, tablet, or computer — including large batches of RAW photos and video.</li>
                        <li>Uploads keep running in the background, so you can keep browsing or close the tab without stopping the transfer.</li>
                        <li>If an upload gets interrupted — a dropped connection, a closed laptop lid, a phone that falls asleep — Keepsake picks up where it left off instead of making you start over.</li>
                        <li>Upload speed automatically adapts to your connection, so it stays fast on Wi-Fi and still works reliably on a slow mobile network.</li>
                        <li>Every file is checked after it arrives to catch anything that came through incomplete or corrupted, and exact duplicates are skipped automatically so you don&apos;t end up with two copies of the same photo.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Find Any Photo in Seconds</h3>
                    <ul>
                        <li>Browse your library sorted by date, or filter by capture date, rating, and likes.</li>
                        <li>Search the way you&apos;d describe a memory — “red dress,” “birthday cake,” “dog at the beach” — and Keepsake understands the subject and its description together, not just keyword matches.</li>
                        <li>Search draws on everything Keepsake already knows about a photo: auto-generated tags and captions, text spotted in the image, the people in it, and where it was taken.</li>
                        <li>Every photo&apos;s detail view shows its map location, camera settings, and processing status at a glance — and you can queue or retry any missing step from the Tools page without re-uploading.</li>
                        <li>iPhone HEIC photos and Canon RAW (CR3) files preview correctly right in your browser, with no extra software or conversion needed.</li>
                        <li>A bell and toast notification let you know as soon as a background job — like tagging or face detection — finishes.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Recognize the People You Love</h3>
                    <ul>
                        <li>Keepsake automatically groups the faces it detects into people, so you can search a name and instantly pull up every photo they&apos;re in.</li>
                        <li>Merge two groups that are the same person, with one-click undo if you change your mind — or split a group apart if it mixed up two different people.</li>
                        <li>Not sure about a match? Confirm or reject individual low-confidence detections, or manually assign faces Keepsake couldn&apos;t group on its own.</li>
                        <li>You can make face-matching stricter or looser to suit your library, and it takes effect immediately — no update or redeploy needed.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Keep Everything Organized</h3>
                    <ul>
                        <li>Build albums by hand, or let smart albums fill themselves automatically by location, date, person, or subject as new matching photos come in.</li>
                        <li>Download a selection of photos, or a whole album, packaged up and ready to save elsewhere.</li>
                        <li>Uploads that fail or come through corrupted are set aside on their own page instead of silently vanishing, so you always know what needs another look.</li>
                        <li>A processing health view shows which photos are missing thumbnails, AI tags, or location/face data, so gaps are easy to find and fix.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Share Your Library With Family</h3>
                    <ul>
                        <li>Turn any album into a public, read-only link for anyone to view — no account needed — and optionally lock it with an access code.</li>
                        <li>Invite up to 15 people into your library as equal members, so the whole family can upload to and browse the same shared collection.</li>
                        <li>Anyone you invite can choose to start their own separate library instead, right from the same invite.</li>
                        <li>Invite links are single-use and expire after 72 hours for safety, and any member can leave a shared library whenever they want.</li>
                        <li>&quot;Clean library&quot; wipes all content while keeping the account itself — it needs the owner and a second member to both confirm, so it can&apos;t happen by accident.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>Your Photos, Your Rules</h3>
                    <ul>
                        <li>Sign in with email and password, or with your Microsoft work account if your organization uses Entra ID.</li>
                        <li>No matter how you access it — gallery, search, or a shared link — you only ever see photos that belong to your library.</li>
                        <li>Links used to upload or view your original files are generated fresh for each request and expire quickly, so they can&apos;t be reused outside their intended purpose.</li>
                        <li>Because everything runs in your own Azure subscription, the parts of the app you&apos;re not using can power down automatically, keeping hosting costs close to zero between visits.</li>
                    </ul>
                </article>

                <article className="additional-card">
                    <h3>What We&apos;re Improving</h3>
                    <ul>
                        <li>Duplicate detection catches exact copies today; catching near-duplicate shots (like two takes of the same photo) is planned as a background check that won&apos;t slow down your uploads.</li>
                        <li>Browsing very large libraries is next in line for a faster, dedicated search index, so performance stays snappy as a library grows into the tens of thousands of photos.</li>
                        <li>“Find similar photos” search will get faster once photo embeddings are precomputed and ready to query directly.</li>
                        <li>We&apos;re adding safeguards so that if an automatic re-clustering run fails partway through, it can&apos;t accidentally hide people you&apos;ve already identified and named.</li>
                        <li>When part of the app has powered down to save cost, the UI will soon tell you it&apos;s warming back up instead of just looking slow.</li>
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

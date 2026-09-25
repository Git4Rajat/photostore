import React, { useEffect, useState } from 'react';
import { useStore } from '../store';
import PhotoGrid from '../components/PhotoGrid';
import { useAppServices } from '../../../components/AppServicesProvider';
import type { BrowserProcessingAction } from '../../../components/AppServicesProvider';
import { getTools, postAdmin } from '../../../services/apiClient';
import type { Photo } from '../types';

const TABS = ['Overview', 'Workbench', 'Recovery', 'History'];

// Prototype step labels -> the browser-processing actions AppServicesProvider runs.
const STEP_ACTIONS: Record<string, BrowserProcessingAction> = {
    Thumbnails: 'thumbnails',
    OCR: 'ocr',
    Faces: 'faces',
};

interface HistoryEntry { action?: string; scope?: string; filenameCount?: number; createdAt?: string; }

/** Tools — live pipeline health + a bulk re-run row + recovery/history. */
export const ToolsPage: React.FC = () => {
    const { toast, route, photosByIds } = useStore();
    const { activeJobs, clusteringActive, clusteringStatusLabel, ipworkActive, ipworkStatusLabel, startBrowserProcessing, browserProcessingActive } = useAppServices();
    const workbenchFilenames = (route.params.filenames ?? '').split(',').map((f) => f.trim()).filter(Boolean);
    const [tab, setTab] = useState(workbenchFilenames.length ? 'Workbench' : 'Overview');
    const [steps, setSteps] = useState<string[]>(['OCR', 'Faces']);
    const [wbSteps, setWbSteps] = useState<string[]>(['OCR', 'Faces']);
    const [history, setHistory] = useState<HistoryEntry[]>([]);
    const [busy, setBusy] = useState(false);

    // Resolve deep-linked filenames to photo objects for the Workbench grid,
    // falling back to a minimal record for any not currently loaded.
    const workbenchPhotos: Photo[] = workbenchFilenames.map((filename) => (
        photosByIds([filename])[0] ?? { id: filename, filename, swatch: 's1', dateLabel: '', year: 0, rating: 0, liked: false, placeId: null, personIds: [], tags: [] }
    ));

    useEffect(() => {
        if (tab !== 'History') return;
        void (async () => {
            try {
                const res = await getTools<{ actions?: HistoryEntry[]; history?: HistoryEntry[] }>('/tools/history');
                setHistory(res?.actions ?? res?.history ?? []);
            } catch {
                setHistory([]);
            }
        })();
    }, [tab]);

    const toggleStep = (name: string) =>
        setSteps((prev) => (prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]));
    const toggleWbStep = (name: string) =>
        setWbSteps((prev) => (prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]));

    const runWorkbench = async () => {
        const actions = wbSteps.map((s) => STEP_ACTIONS[s]).filter(Boolean);
        if (!actions.length || !workbenchFilenames.length) return;
        try {
            const queued = await startBrowserProcessing({ actions, filenames: workbenchFilenames, force: true });
            toast(`Re-processing ${queued} photo${queued === 1 ? '' : 's'} · ${wbSteps.join(', ')}`);
        } catch {
            toast('Couldn’t start processing');
        }
    };

    const runSelected = async () => {
        const actions = steps.map((s) => STEP_ACTIONS[s]).filter(Boolean);
        if (!actions.length) return;
        try {
            const queued = await startBrowserProcessing({ actions, force: true });
            toast(`Re-processing ${queued} photo${queued === 1 ? '' : 's'} · ${steps.join(', ')}`);
        } catch {
            toast('Couldn’t start processing');
        }
    };

    const runAdmin = async (label: string, path: string, body: Record<string, unknown>) => {
        setBusy(true);
        try {
            await postAdmin(path, body);
            toast(`${label} started`);
        } catch {
            toast(`Couldn’t start ${label.toLowerCase()}`);
        } finally {
            setBusy(false);
        }
    };

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Tools</h1>
                    <p className="pt-page-sub">Processing pipeline health</p>
                </div>
            </div>

            <div className="pt-subnav">
                {TABS.map((t) => (
                    <button key={t} type="button" className={`pt-subnav-item${tab === t ? ' on' : ''}`} onClick={() => setTab(t)}>{t}</button>
                ))}
            </div>

            {tab === 'Overview' && (
                <>
                    <div className="pt-queue-grid">
                        <div className="card-glass pt-queue-card">
                            <div className="pt-queue-title">Clustering</div>
                            <div className="pt-queue-counts">
                                <span className={`qchip${clusteringActive ? ' warn' : ''}`}>{clusteringActive ? (clusteringStatusLabel || 'Running') : 'Idle'}</span>
                            </div>
                        </div>
                        <div className="card-glass pt-queue-card">
                            <div className="pt-queue-title">Server processing</div>
                            <div className="pt-queue-counts">
                                <span className={`qchip${ipworkActive ? ' warn' : ''}`}>{ipworkActive ? (ipworkStatusLabel || 'Running') : 'Idle'}</span>
                            </div>
                        </div>
                        <div className="card-glass pt-queue-card">
                            <div className="pt-queue-title">Browser AI</div>
                            <div className="pt-queue-counts">
                                <span className={`qchip${browserProcessingActive ? ' warn' : ''}`}>{browserProcessingActive ? 'Running' : 'Idle'}</span>
                            </div>
                        </div>
                        <div className="card-glass pt-queue-card">
                            <div className="pt-queue-title">Active jobs</div>
                            <div className="pt-queue-counts">
                                <span className="qchip">{activeJobs.length}</span>
                            </div>
                        </div>
                    </div>

                    {activeJobs.length > 0 && (
                        <div className="card-glass pt-history">
                            {activeJobs.slice(0, 12).map((job) => (
                                <div key={job.jobId} className="pt-history-row">{job.title || job.kind} · {job.status}{job.message ? ` · ${job.message}` : ''}</div>
                            ))}
                        </div>
                    )}

                    <div className="pt-step-row">
                        {Object.keys(STEP_ACTIONS).map((name) => (
                            <button key={name} type="button" className={`pt-step${steps.includes(name) ? ' on' : ''}`} onClick={() => toggleStep(name)}>{name}</button>
                        ))}
                        <button type="button" className="pt-step run" onClick={() => void runSelected()} disabled={!steps.length || browserProcessingActive}>
                            Run selected ({steps.length})
                        </button>
                    </div>
                </>
            )}

            {tab === 'Workbench' && (
                <>
                    {workbenchFilenames.length === 0 ? (
                        <p className="pt-grid-empty">Select photos in the gallery, then choose “Workbench” to re-run processing on just those photos.</p>
                    ) : (
                        <>
                            <div className="pt-menu-label">{workbenchFilenames.length} photo{workbenchFilenames.length === 1 ? '' : 's'} in this workbench</div>
                            <PhotoGrid photos={workbenchPhotos} />
                            <div className="pt-step-row">
                                {Object.keys(STEP_ACTIONS).map((name) => (
                                    <button key={name} type="button" className={`pt-step${wbSteps.includes(name) ? ' on' : ''}`} onClick={() => toggleWbStep(name)}>{name}</button>
                                ))}
                                <button type="button" className="pt-step run" onClick={() => void runWorkbench()} disabled={!wbSteps.length || browserProcessingActive}>
                                    Run on these ({wbSteps.length})
                                </button>
                            </div>
                        </>
                    )}
                </>
            )}

            {tab === 'Recovery' && (
                <div className="pt-recovery">
                    <div className="card-glass pt-recover-card">
                        <strong>Backfill all photos</strong>
                        <span>Re-run the full pipeline — thumbnails, EXIF, OCR, AI vision, faces.</span>
                        <button type="button" className="btn" disabled={busy} onClick={() => void runAdmin('Backfill', '/api/admin/backfill/photos', { repair: true })}>Run backfill</button>
                    </div>
                    <div className="card-glass pt-recover-card">
                        <strong>Deduplicate faces</strong>
                        <span>Find and remove duplicate face rows across your library.</span>
                        <button type="button" className="btn mock-cta" disabled={busy} onClick={() => void runAdmin('Face dedupe', '/api/admin/people/dedupe', { apply: true })}>Apply</button>
                    </div>
                    <div className="card-glass pt-recover-card">
                        <strong>Rebuild people index</strong>
                        <span>Recompute the people clustering index from current face data.</span>
                        <button type="button" className="btn" disabled={busy} onClick={() => void runAdmin('People index rebuild', '/api/admin/people/rebuild', {})}>Rebuild</button>
                    </div>
                </div>
            )}

            {tab === 'History' && (
                <div className="card-glass pt-history">
                    {history.length === 0 ? (
                        <div className="pt-history-row">No recent actions.</div>
                    ) : (
                        history.map((h, i) => (
                            <div key={i} className="pt-history-row">
                                {h.action ?? 'Action'} · {h.scope ?? 'library'}
                                {typeof h.filenameCount === 'number' ? ` · ${h.filenameCount} photos` : ''}
                                {h.createdAt ? ` · ${new Date(h.createdAt).toLocaleString()}` : ''}
                            </div>
                        ))
                    )}
                </div>
            )}
        </div>
    );
};

export default ToolsPage;

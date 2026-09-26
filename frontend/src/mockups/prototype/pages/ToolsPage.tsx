import React, { useEffect, useState } from 'react';
import { ArrowsPointingOutIcon, InformationCircleIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import WorkbenchGrid from '../components/WorkbenchGrid';
import { useAppServices } from '../../../components/AppServicesProvider';
import type { BrowserProcessingAction } from '../../../components/AppServicesProvider';
import { getTools, postTools, postAdmin, getExtras } from '../../../services/apiClient';
import { getRuntimeConfig } from '../../../config/appConfig';
import type { Photo } from '../types';

const TABS = ['Overview', 'Workbench', 'Recovery', 'History', 'Diagnostics'];

// Prototype step labels -> the browser-processing actions AppServicesProvider
// runs. Order here is the pipeline order the buttons render in.
const STEP_ACTIONS: Record<string, BrowserProcessingAction> = {
    Preview: 'preview',
    Thumbnails: 'thumbnails',
    EXIF: 'exif',
    OCR: 'ocr',
    Vision: 'vision',
    Geo: 'map',
    Faces: 'faces',
};

// Steps that need the on-device AI model; selecting one triggers a model load so
// the run isn't silently stuck at 'pending' waiting for a model that never loads.
const AI_STEPS = new Set<BrowserProcessingAction>(['ocr', 'vision', 'faces']);

interface HistoryEntry { actionId?: string; action?: string; steps?: string[]; scope?: string; filenameCount?: number; createdAt?: string; }

interface PeopleDiagnostic {
    totalFaces: number;
    acceptedForClustering: number;
    rejectedFaces: number;
    suspiciousFaces: number;
    staleEmbeddingVersionFaces: number;
    noEmbeddingFaces: number;
    unassignedFaces: number;
    confirmedFaces: number;
    totalPeople: number;
    clusteringConfiguration: {
        browserOnlyProcessing: boolean;
        clusteringQueueAvailable: boolean;
        activeClusteringJob?: boolean;
    };
    recommendation?: string;
}

/** Tools — live pipeline health + a bulk re-run row + recovery/history. */
export const ToolsPage: React.FC = () => {
    const { toast, route, photos, navigate } = useStore();
    const {
        activeJobs, clusteringActive, clusteringStatusLabel, ipworkActive, ipworkStatusLabel,
        startBrowserProcessing, browserProcessingActive, browserAiModelState, loadBrowserAiModel,
    } = useAppServices();
    const backendMode = getRuntimeConfig().processingMode === 'backend';
    const workbenchFilenames = (route.params.filenames ?? '').split(',').map((f) => f.trim()).filter(Boolean);
    const [tab, setTab] = useState(workbenchFilenames.length ? 'Workbench' : 'Overview');
    const [steps, setSteps] = useState<string[]>(['OCR', 'Faces']);
    const [wbSteps, setWbSteps] = useState<string[]>(['OCR', 'Faces']);
    const [wbSelection, setWbSelection] = useState<string[]>(workbenchFilenames);
    const [history, setHistory] = useState<HistoryEntry[]>([]);
    const [busy, setBusy] = useState(false);
    const [reselectingId, setReselectingId] = useState<string | null>(null);
    const [diagnostic, setDiagnostic] = useState<PeopleDiagnostic | null>(null);
    const [diagnosticLoading, setDiagnosticLoading] = useState(false);

    // A deep link ("Open in Workbench" from the gallery) pre-selects those
    // photos, but any photo in the loaded library can be searched for and
    // selected here too.
    useEffect(() => {
        if (workbenchFilenames.length) {
            setWbSelection(workbenchFilenames);
            setTab('Workbench');
        }
    }, [route.params.filenames]); // eslint-disable-line react-hooks/exhaustive-deps

    const reselectHistoryAction = async (actionId?: string) => {
        if (!actionId) return;
        setReselectingId(actionId);
        try {
            const res = await getTools<{ filenames?: string[] }>(`/api/tools/workbench/actions/${actionId}`);
            const filenames = Array.isArray(res?.filenames) ? res.filenames : [];
            if (!filenames.length) {
                toast("Those photos aren't available anymore");
                return;
            }
            navigate('tools', { filenames: filenames.join(',') });
        } catch {
            toast('Couldn’t reselect those photos');
        } finally {
            setReselectingId(null);
        }
    };

    const loadDiagnostic = async () => {
        setDiagnosticLoading(true);
        try {
            const res = await getExtras<PeopleDiagnostic>('/api/people/diagnostic');
            setDiagnostic(res ?? null);
        } catch {
            setDiagnostic(null);
        } finally {
            setDiagnosticLoading(false);
        }
    };

    useEffect(() => {
        if (tab !== 'Diagnostics') return;
        void loadDiagnostic();
    }, [tab]);

    const toggleWbSelect = (id: string) =>
        setWbSelection((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

    // Any deep-linked filename not present in the loaded library yet still
    // needs a minimal record so it can render as a tile.
    const missingDeepLinked: Photo[] = workbenchFilenames
        .filter((filename) => !photos.some((p) => p.id === filename))
        .map((filename) => ({ id: filename, filename, swatch: 's1', dateLabel: '', year: 0, rating: 0, liked: false, placeId: null, personIds: [], tags: [] }));
    const workbenchLibrary: Photo[] = [...missingDeepLinked, ...photos];

    const loadHistory = async () => {
        try {
            const res = await getTools<{ actions?: HistoryEntry[] }>('/api/tools/workbench/actions');
            setHistory(Array.isArray(res?.actions) ? res.actions : []);
        } catch {
            setHistory([]);
        }
    };

    useEffect(() => {
        if (tab !== 'History') return;
        void loadHistory();
    }, [tab]);

    const toggleStep = (name: string) =>
        setSteps((prev) => (prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]));
    const toggleWbStep = (name: string) =>
        setWbSteps((prev) => (prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]));

    // Log a history row so History reflects the run even for purely in-browser
    // processing (which otherwise never touches the backend). Best-effort.
    const recordAction = (stepNames: string[], scope: 'selected' | 'library', filenames?: string[]) => {
        void postTools('/api/tools/workbench/actions', {
            action: 'reprocess',
            steps: stepNames,
            scope,
            filenameCount: filenames?.length ?? 0,
            filenames: filenames ?? [],
            force: true,
        }).catch(() => { /* logging must never block the run */ });
    };

    // Kick a model load when the run needs AI steps but the model isn't ready,
    // so ocr/vision/faces don't sit at 'pending' forever waiting for a click.
    const ensureModelForActions = (actions: BrowserProcessingAction[]) => {
        if (backendMode) return;
        if (!actions.some((a) => AI_STEPS.has(a))) return;
        if (browserAiModelState.status !== 'available') void loadBrowserAiModel();
    };

    const runWorkbench = async () => {
        const actions = wbSteps.map((s) => STEP_ACTIONS[s]).filter(Boolean);
        if (!actions.length || !wbSelection.length) return;
        ensureModelForActions(actions);
        try {
            const queued = await startBrowserProcessing({ actions, filenames: wbSelection, force: true });
            recordAction(wbSteps, 'selected', wbSelection);
            toast(queued > 0
                ? `Re-processing ${queued} photo${queued === 1 ? '' : 's'} · ${wbSteps.join(', ')}`
                : `Queued ${wbSelection.length} photo${wbSelection.length === 1 ? '' : 's'} · ${wbSteps.join(', ')}`);
        } catch {
            toast('Couldn’t start processing');
        }
    };

    const runSelected = async () => {
        const actions = steps.map((s) => STEP_ACTIONS[s]).filter(Boolean);
        if (!actions.length) return;
        if (backendMode) {
            toast('This deployment processes on the server — use Recovery → Backfill to re-run the whole library.');
            return;
        }
        ensureModelForActions(actions);
        try {
            const queued = await startBrowserProcessing({ actions, force: true });
            recordAction(steps, 'library');
            toast(queued > 0
                ? `Re-processing ${queued} photo${queued === 1 ? '' : 's'} · ${steps.join(', ')}`
                : `Started ${steps.join(', ')} — pulling pending photos…`);
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
                    <div className="pt-menu-label">
                        Search or sort your library, tick the photos you want, then force a re-run of any step —
                        each tile shows how all 7 steps did and an <InformationCircleIcon className="pt-inline-icon" /> for its EXIF + tags.
                    </div>
                    <WorkbenchGrid
                        photos={workbenchLibrary}
                        selection={wbSelection}
                        onToggleSelect={toggleWbSelect}
                        onSelectMany={setWbSelection}
                    />
                    <div className="pt-step-row wb-force">
                        {Object.keys(STEP_ACTIONS).map((name) => (
                            <button key={name} type="button" className={`pt-step${wbSteps.includes(name) ? ' on' : ''}`} onClick={() => toggleWbStep(name)}>{name}</button>
                        ))}
                        <button type="button" className="pt-step run" onClick={() => void runWorkbench()} disabled={!wbSteps.length || !wbSelection.length || browserProcessingActive}>
                            Force re-run ({wbSelection.length} selected)
                        </button>
                    </div>
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
                            <div key={h.actionId ?? i} className="pt-history-row pt-history-row-with-action">
                                <span>
                                    {(h.action ?? 'Action')} · {h.scope ?? 'library'}
                                    {Array.isArray(h.steps) && h.steps.length ? ` · ${h.steps.join(', ')}` : ''}
                                    {typeof h.filenameCount === 'number' && h.filenameCount > 0 ? ` · ${h.filenameCount} photos` : ''}
                                    {h.createdAt ? ` · ${new Date(h.createdAt).toLocaleString()}` : ''}
                                </span>
                                {h.actionId && (
                                    <button
                                        type="button"
                                        className="pt-history-reselect"
                                        title="Reselect these photos"
                                        aria-label="Reselect these photos"
                                        disabled={reselectingId === h.actionId}
                                        onClick={() => void reselectHistoryAction(h.actionId)}
                                    >
                                        <ArrowsPointingOutIcon />
                                    </button>
                                )}
                            </div>
                        ))
                    )}
                </div>
            )}

            {tab === 'Diagnostics' && (
                <div className="pt-recovery">
                    <div className="card-glass pt-recover-card pt-diagnostic-card">
                        <div className="pt-toolbar">
                            <strong>Why aren't faces clustering into people?</strong>
                            <button type="button" className="btn" disabled={diagnosticLoading} onClick={() => void loadDiagnostic()}>
                                {diagnosticLoading ? 'Refreshing…' : 'Refresh'}
                            </button>
                        </div>
                        {diagnosticLoading && !diagnostic && <span>Running diagnostics…</span>}
                        {!diagnosticLoading && !diagnostic && <span>Couldn’t load diagnostics.</span>}
                        {diagnostic && (
                            <>
                                {diagnostic.recommendation && <span className="pt-diagnostic-recommendation">{diagnostic.recommendation}</span>}
                                <div className="pt-diagnostic-chips">
                                    <span className="qchip">Total faces · {diagnostic.totalFaces}</span>
                                    <span className="qchip">Accepted · {diagnostic.acceptedForClustering}</span>
                                    <span className="qchip">Unassigned · {diagnostic.unassignedFaces}</span>
                                    <span className="qchip">Confirmed · {diagnostic.confirmedFaces}</span>
                                    <span className="qchip">Rejected · {diagnostic.rejectedFaces}</span>
                                    <span className="qchip">Suspicious · {diagnostic.suspiciousFaces}</span>
                                    <span className="qchip">Stale embedding · {diagnostic.staleEmbeddingVersionFaces}</span>
                                    <span className="qchip">No embedding · {diagnostic.noEmbeddingFaces}</span>
                                    <span className="qchip">Total people · {diagnostic.totalPeople}</span>
                                </div>
                                <div className="pt-diagnostic-chips">
                                    <span className="qchip">Clustering queue · {diagnostic.clusteringConfiguration.clusteringQueueAvailable ? 'available' : 'unavailable'}</span>
                                    <span className="qchip">Browser-only processing · {diagnostic.clusteringConfiguration.browserOnlyProcessing ? 'yes' : 'no'}</span>
                                    <span className="qchip">Active clustering job · {diagnostic.clusteringConfiguration.activeClusteringJob ? 'yes' : 'no'}</span>
                                </div>
                            </>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
};

export default ToolsPage;

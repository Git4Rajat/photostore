import React, { useState } from 'react';
import { useStore } from '../store';
import { QUEUE_STAGES } from '../data';

const TABS = ['Overview', 'Queue status', 'Recovery', 'History'];

/** Tools — pipeline health overview + a bulk re-run row + recovery/history. */
export const ToolsPage: React.FC = () => {
    const { toast } = useStore();
    const [tab, setTab] = useState('Overview');
    const [steps, setSteps] = useState<string[]>(['OCR', 'Faces']);

    const toggleStep = (name: string) =>
        setSteps((prev) => (prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]));

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

            {(tab === 'Overview' || tab === 'Queue status') && (
                <>
                    <div className="pt-queue-grid">
                        {QUEUE_STAGES.map((s) => (
                            <div key={s.name} className="card-glass pt-queue-card">
                                <div className="pt-queue-title">{s.name}</div>
                                <div className="pt-queue-counts">
                                    <span className="qchip">wait {s.wait}</span>
                                    <span className="qchip">run {s.run}</span>
                                    <span className={`qchip${s.noData ? ' warn' : ''}`}>no data {s.noData}</span>
                                    <span className={`qchip${s.fail ? ' warn' : ''}`}>fail {s.fail}</span>
                                </div>
                            </div>
                        ))}
                    </div>
                    <div className="pt-step-row">
                        {['Thumbnails', 'OCR', 'Faces', 'Map'].map((name) => (
                            <button key={name} type="button" className={`pt-step${steps.includes(name) ? ' on' : ''}`} onClick={() => toggleStep(name)}>{name}</button>
                        ))}
                        <button type="button" className="pt-step run" onClick={() => toast(`Re-running ${steps.length} step${steps.length === 1 ? '' : 's'} on the library`)} disabled={!steps.length}>
                            Run selected ({steps.length})
                        </button>
                    </div>
                </>
            )}

            {tab === 'Recovery' && (
                <div className="pt-recovery">
                    <div className="card-glass pt-recover-card">
                        <strong>Backfill all photos</strong>
                        <span>Re-run the full pipeline — thumbnails, EXIF, OCR, AI vision, map tagging, faces.</span>
                        <button type="button" className="btn" onClick={() => toast('Backfill queued')}>Run backfill</button>
                    </div>
                    <div className="card-glass pt-recover-card">
                        <strong>Deduplicate faces</strong>
                        <span>Preview: 14 duplicate rows across 9 photos.</span>
                        <button type="button" className="btn mock-cta" onClick={() => toast('Deduplicated 14 face rows')}>Apply</button>
                    </div>
                </div>
            )}

            {tab === 'History' && (
                <div className="card-glass pt-history">
                    <div className="pt-history-row">Repair people clusters · library · 2,481 photos · Sep 21, 4:12pm</div>
                    <div className="pt-history-row">Rebuild vector index · library · Sep 19, 9:03am</div>
                    <div className="pt-history-row">Deduplicate faces · selected · 9 photos · Sep 14, 6:40pm</div>
                </div>
            )}
        </div>
    );
};

export default ToolsPage;

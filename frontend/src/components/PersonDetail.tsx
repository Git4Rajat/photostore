import React, { useEffect, useMemo, useState } from 'react';
import { ArrowLeftIcon, ArrowUturnLeftIcon, ArrowsRightLeftIcon, CheckIcon, ExclamationTriangleIcon, MinusCircleIcon, NoSymbolIcon } from '@heroicons/react/24/outline';
import { useParams, useNavigate } from 'react-router-dom';
import faceService from '../services/faceService';
import { resolveApiUrl } from '../services/apiClient';
import { showToast } from '../services/toast';
import { useProtectedBlobUrls } from '../services/imageClient';
import type { PersonDetailModel, PersonFace, PersonSummary } from '../types/people';

const SUSPICIOUS_FACE_CONFIDENCE = 0.6;
const isSuspiciousFace = (face: PersonFace) => face?.reviewStatus === 'suspicious' || Number(face?.confidence || 0) < SUSPICIOUS_FACE_CONFIDENCE;

interface FaceDetectionImageProps {
    face: PersonFace;
    src: string;
}

const getFiniteNumber = (value: unknown, fallback = 0) => {
    const next = Number(value);
    return Number.isFinite(next) ? next : fallback;
};

const FaceDetectionImage: React.FC<FaceDetectionImageProps> = ({ face, src }) => {
    const mediaRef = React.useRef<HTMLDivElement | null>(null);
    const [mediaSize, setMediaSize] = useState({ width: 0, height: 0 });
    const [naturalSize, setNaturalSize] = useState({
        width: getFiniteNumber(face.imageWidth),
        height: getFiniteNumber(face.imageHeight),
    });

    useEffect(() => {
        const node = mediaRef.current;
        if (!node) {
            return undefined;
        }

        const updateSize = () => {
            const rect = node.getBoundingClientRect();
            setMediaSize({ width: rect.width, height: rect.height });
        };

        updateSize();
        if (typeof ResizeObserver !== 'undefined') {
            const observer = new ResizeObserver(updateSize);
            observer.observe(node);
            return () => observer.disconnect();
        }

        window.addEventListener('resize', updateSize);
        return () => window.removeEventListener('resize', updateSize);
    }, []);

    const bbox = face.bbox || {};
    const sourceWidth = getFiniteNumber(face.imageWidth) || naturalSize.width;
    const sourceHeight = getFiniteNumber(face.imageHeight) || naturalSize.height;
    const faceLeft = getFiniteNumber(bbox.left);
    const faceTop = getFiniteNumber(bbox.top);
    const faceWidth = getFiniteNumber(bbox.width);
    const faceHeight = getFiniteNumber(bbox.height);
    const canPlaceBox = (
        mediaSize.width > 0
        && mediaSize.height > 0
        && sourceWidth > 0
        && sourceHeight > 0
        && faceWidth > 0
        && faceHeight > 0
    );

    const boxStyle = useMemo<React.CSSProperties | undefined>(() => {
        if (!canPlaceBox) {
            return undefined;
        }
        const scale = Math.max(mediaSize.width / sourceWidth, mediaSize.height / sourceHeight);
        const renderedWidth = sourceWidth * scale;
        const renderedHeight = sourceHeight * scale;
        const offsetX = (mediaSize.width - renderedWidth) / 2;
        const offsetY = (mediaSize.height - renderedHeight) / 2;
        return {
            left: `${offsetX + faceLeft * scale}px`,
            top: `${offsetY + faceTop * scale}px`,
            width: `${faceWidth * scale}px`,
            height: `${faceHeight * scale}px`,
        };
    }, [canPlaceBox, faceHeight, faceLeft, faceTop, faceWidth, mediaSize.height, mediaSize.width, sourceHeight, sourceWidth]);

    return (
        <div ref={mediaRef} className="person-face-media-frame">
            <img
                src={src}
                alt={face.filename}
                className="photo-media"
                onLoad={(event) => {
                    const image = event.currentTarget;
                    setNaturalSize({
                        width: image.naturalWidth || sourceWidth,
                        height: image.naturalHeight || sourceHeight,
                    });
                }}
            />
            {boxStyle && <span className="person-face-detection-box" style={boxStyle} aria-hidden="true" />}
        </div>
    );
};

const PersonDetail: React.FC = () => {
    const { personId } = useParams<{ personId: string }>();
    const [person, setPerson] = useState<PersonDetailModel | null>(null);
    const [name, setName] = useState('');
    const [loading, setLoading] = useState(false);
    const navigate = useNavigate();
    const [otherPersons, setOtherPersons] = useState<PersonSummary[]>([]);
    const [mergeCandidatesLoading, setMergeCandidatesLoading] = useState(false);
    const [selectedMerge, setSelectedMerge] = useState<Record<string, boolean>>({});
    const [lastMergeId, setLastMergeId] = useState<string | null>(null);
    const displayFaces = useMemo(() => {
        const seen = new Set<string>();
        return (person?.faces || []).filter((face) => {
            const bbox = face?.bbox || {};
            const key = [
                face?.filename || '',
                Math.round(Number(bbox.left || 0)),
                Math.round(Number(bbox.top || 0)),
                Math.round(Number(bbox.width || 0)),
                Math.round(Number(bbox.height || 0)),
                Number(face?.imageWidth || 0),
                Number(face?.imageHeight || 0),
            ].join('|');
            if (seen.has(key)) {
                return false;
            }
            seen.add(key);
            return true;
        });
    }, [person?.faces]);
    const thumbnailPaths = displayFaces
        .map((f) => f.filename)
        .filter((filename): filename is string => Boolean(filename))
        .map((filename) => `/api/photos/thumbnail/${encodeURIComponent(filename)}`);
    const protectedImageUrls = useProtectedBlobUrls(thumbnailPaths);

    const loadPerson = async () => {
        if (!personId) return;
        try {
            const res = await faceService.getPerson(personId);
            setPerson(res as PersonDetailModel);
            setName(res.name || '');
        } catch (e: unknown) {
            setPerson(null);
        }
    };

    const loadMergeCandidates = async () => {
        if (!personId) return;
        setMergeCandidatesLoading(true);
        try {
            const all = await faceService.listPersons();
            setOtherPersons(((all.persons || []) as PersonSummary[]).filter((pp) => pp.personId !== personId));
        } catch (e: unknown) {
            setOtherPersons([]);
        } finally {
            setMergeCandidatesLoading(false);
        }
    };

    const load = async () => {
        if (!personId) return;
        setLoading(true);
        try {
            await loadPerson();
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        setOtherPersons([]);
        setSelectedMerge({});
        void (async () => {
            await load();
            void loadMergeCandidates();
        })();
    }, [personId]);

    const handleSave = async () => {
        if (!personId) return;
        setLoading(true);
        try {
            await faceService.labelPerson(personId, name);
            setPerson((prev) => (prev ? { ...prev, name } : prev));
            setName(name);
            showToast('Name saved');
        } catch (e: unknown) {
            // ignore
        } finally {
            setLoading(false);
        }
    };

    const handleSeparate = async (faceId: string) => {
        if (!personId) return;
        setLoading(true);
        let navigated = false;
        try {
            const res = await faceService.separateFace(personId, faceId);
            const nextPersonId = String(res?.personId || '');
            showToast(`Face moved to ${res?.name || 'own cluster'}`);
            if (nextPersonId && res?.oldPersonDeleted) {
                navigated = true;
                navigate(`/people/${nextPersonId}`);
                return;
            }
            await load();
            void loadMergeCandidates();
        } catch (e: unknown) {
            showToast(String(e));
        } finally {
            if (!navigated) {
                setLoading(false);
            }
        }
    };

    const handleConfirmFace = async (faceId: string) => {
        if (!personId) return;
        setLoading(true);
        try {
            await faceService.confirmFace(personId, faceId);
            setPerson((prev) => (prev ? {
                ...prev,
                faces: (prev.faces || []).map((face) => (
                    face.faceId === faceId ? { ...face, reviewStatus: 'confirmed' } : face
                )),
            } : prev));
            showToast('Face confirmed');
        } catch (e: unknown) {
            showToast(String(e));
        } finally {
            setLoading(false);
        }
    };

    const handleNotFace = async (faceId: string) => {
        if (!personId) return;
        setLoading(true);
        try {
            const res = await faceService.markNotFace(personId, faceId);
            if (res?.personDeleted) {
                showToast('False positive removed');
                navigate('/people');
                return;
            }
            setPerson((prev) => (prev ? { ...prev, faces: (prev.faces || []).filter((face) => face.faceId !== faceId) } : prev));
            showToast('Marked as not a face');
        } catch (e: unknown) {
            showToast(String(e));
        } finally {
            setLoading(false);
        }
    };

    const handleMergeSelected = async () => {
        if (!personId) return;
        const toMerge = Object.keys(selectedMerge).filter(id => selectedMerge[id]);
        if (toMerge.length === 0) return;
        if (!window.confirm(`Merge ${toMerge.length} persons into this person?`)) return;
        setLoading(true);
        try {
            const res = await faceService.mergePersons(personId, toMerge);
            setLastMergeId(res.mergeId || null);
            setOtherPersons((prev) => prev.filter((candidate) => !toMerge.includes(candidate.personId)));
            setSelectedMerge({});
            await load();
            void loadMergeCandidates();
            showToast('Merge completed');
        } catch (e: unknown) {
            // ignore
        } finally {
            setLoading(false);
        }
    };

    const handleUndoMerge = async () => {
        if (!lastMergeId) return;
        setLoading(true);
        try {
            await faceService.undoMerge(lastMergeId);
            setLastMergeId(null);
            await load();
            void loadMergeCandidates();
            showToast('Merge undone');
        } catch (e: unknown) {
            // ignore
        } finally {
            setLoading(false);
        }
    };

    if (!personId) return <div className="people-empty">No person selected.</div>;

    return (
        <section className="card-glass person-detail">
            <div className="person-detail-header">
                <div className="person-detail-title">
                    <p className="person-detail-kicker">Person</p>
                    <h2 className="person-detail-name">{name.trim() || person?.name || 'Unnamed'}</h2>
                    <p className="person-detail-meta">{displayFaces.length} faces</p>
                </div>
                <div className="person-detail-actions">
                    <button className="btn btn-soft icon-btn" onClick={() => navigate('/people')} aria-label="Back to people">
                        <ArrowLeftIcon className="toolbar-icon" />
                        <span className="sr-only">Back to people</span>
                    </button>
                </div>
            </div>

            {loading && <div className="people-status">Loading...</div>}

            {person && (
                <div className="person-detail-body">
                    <div className="person-detail-toolbar">
                        <input
                            className="field person-detail-input"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            placeholder="Name"
                        />
                        <button className="btn btn-primary icon-btn" onClick={handleSave} aria-label="Save name">
                            <CheckIcon className="toolbar-icon" />
                            <span className="sr-only">Save name</span>
                        </button>
                    </div>

                    {displayFaces.length === 0 ? (
                        <div className="people-empty">No faces found for this person yet.</div>
                    ) : (
                        <div className="gallery-grid person-face-grid">
                            {displayFaces.map((f: PersonFace) => {
                                const faceId = f.faceId || '';
                                const path = f.filename ? `/api/photos/thumbnail/${encodeURIComponent(f.filename)}` : '';
                                const thumbSrc = protectedImageUrls[path] || resolveApiUrl(path);
                                const suspicious = isSuspiciousFace(f);
                                const singleFaceCluster = displayFaces.length === 1 && (person?.name || '').toLowerCase().startsWith('unnamed');
                                return (
                                    <div key={faceId || `${f.filename}-${f.bbox?.left}-${f.bbox?.top}`} className={`photo-card person-face-card ${suspicious ? 'is-suspicious-face' : ''}`}>
                                        <div className="person-face-media">
                                            {thumbSrc ? (
                                                <FaceDetectionImage face={f} src={thumbSrc} />
                                            ) : (
                                                <div
                                                    className="photo-media"
                                                    style={{
                                                        display: 'flex',
                                                        alignItems: 'center',
                                                        justifyContent: 'center',
                                                        color: '#777',
                                                        background: '#f3f4f6',
                                                        minHeight: 120,
                                                    }}
                                                >
                                                    Loading thumbnail...
                                                </div>
                                            )}
                                        </div>
                                        <div className="photo-body">
                                            <div className="photo-title-row">
                                                <p className="photo-name">{f.filename}</p>
                                                <div className="person-face-meta-chips">
                                                    {suspicious && (
                                                        <span className="person-face-warning" title={f.suspiciousReason || 'Detector confidence is low'}>
                                                            <ExclamationTriangleIcon />
                                                            Review
                                                        </span>
                                                    )}
                                                    {typeof f.confidence === 'number' && (
                                                        <p className="photo-kind" title="Detector confidence, not identity confidence">
                                                            Det {Math.round(f.confidence * 100)}%
                                                        </p>
                                                    )}
                                                </div>
                                            </div>
                                            <div className="photo-row person-face-actions">
                                                <button className="btn btn-link icon-btn" onClick={() => faceId && handleConfirmFace(faceId)} disabled={!faceId} aria-label="Confirm face">
                                                    <CheckIcon className="toolbar-icon" />
                                                    <span className="sr-only">Confirm face</span>
                                                </button>
                                                <button
                                                    className={`btn ${singleFaceCluster ? 'btn-danger' : 'btn-link'} icon-btn`}
                                                    onClick={() => faceId && handleNotFace(faceId)}
                                                    disabled={!faceId}
                                                    aria-label="Not a face"
                                                    title="Not a face"
                                                >
                                                    <NoSymbolIcon className="toolbar-icon" />
                                                    <span className="sr-only">Not a face</span>
                                                </button>
                                                <button className="btn btn-link icon-btn" onClick={() => faceId && handleSeparate(faceId)} disabled={!faceId} aria-label="Move face to own cluster" title="Move face to own cluster">
                                                    <MinusCircleIcon className="toolbar-icon" />
                                                    <span className="sr-only">Move face to own cluster</span>
                                                </button>
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    )}

                    <div className="people-panel person-merge-panel">
                        <div className="people-panel-header">
                            <div>
                                <p className="people-panel-title">Merge people</p>
                                <p className="people-panel-meta">Merge other clusters into this person.</p>
                            </div>
                            <div className="person-merge-actions">
                                <button className="btn btn-danger icon-btn" onClick={handleMergeSelected} disabled={loading} aria-label="Merge selected">
                                    <ArrowsRightLeftIcon className="toolbar-icon" />
                                    <span className="sr-only">Merge selected</span>
                                </button>
                                {lastMergeId && (
                                    <button className="btn btn-link icon-btn" onClick={handleUndoMerge} aria-label="Undo merge">
                                        <ArrowUturnLeftIcon className="toolbar-icon" />
                                        <span className="sr-only">Undo merge</span>
                                    </button>
                                )}
                            </div>
                        </div>
                        {mergeCandidatesLoading && <div className="people-empty">Loading merge options...</div>}
                        {!mergeCandidatesLoading && otherPersons.length === 0 && <div className="people-empty">No other persons available.</div>}
                        {otherPersons.length > 0 && (
                            <div className="person-merge-list">
                                {otherPersons.map(op => (
                                    <label key={op.personId} className="person-merge-row">
                                        <input
                                            type="checkbox"
                                            checked={!!selectedMerge[op.personId]}
                                            onChange={(e) => setSelectedMerge(prev => ({ ...prev, [op.personId]: e.target.checked }))}
                                        />
                                        <span className="person-merge-name">{op.name || 'Unnamed'}</span>
                                        <span className="person-merge-count">{(op.faceIds || []).length} faces</span>
                                    </label>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            )}

            {!person && !loading && <div className="people-empty">Person not found.</div>}
        </section>
    );
};

export default PersonDetail;

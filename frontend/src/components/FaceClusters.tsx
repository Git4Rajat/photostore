import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowsRightLeftIcon, ArrowRightIcon, CheckIcon, ExclamationTriangleIcon, MagnifyingGlassIcon, SparklesIcon, TrashIcon, UserCircleIcon, XMarkIcon } from '@heroicons/react/24/outline';
import faceService from '../services/faceService';
import { getUploadJson, resolveApiUrl } from '../services/apiClient';
import { showToast } from '../services/toast';
import { isAuthEnabled } from '../services/authClient';
import { useNavigate } from 'react-router-dom';
import type { MergeHistoryItem, PersonFace, PersonSummary, SuggestionItem } from '../types/people';

const SUSPICIOUS_FACE_CONFIDENCE = 0.6;
const isSuspiciousFace = (face?: PersonFace) => face?.reviewStatus === 'suspicious' || Number(face?.confidence || 0) < SUSPICIOUS_FACE_CONFIDENCE;
const getFaceFallbackPath = (filename?: string | null) => filename ? `/api/photos/access/thumbnail/${encodeURIComponent(filename)}` : '';
const getFaceProxyThumbnailPath = (filename?: string | null) => filename ? `/api/photos/thumbnail/${encodeURIComponent(filename)}` : '';

const getFaceObjectPosition = (rep?: PersonFace) => {
    const bbox = rep?.bbox;
    const width = Number(rep?.imageWidth || 0);
    const height = Number(rep?.imageHeight || 0);
    if (!bbox || width <= 0 || height <= 0) {
        return '50% 35%';
    }
    const centerX = Number(bbox.left || 0) + Number(bbox.width || 0) / 2;
    const centerY = Number(bbox.top || 0) + Number(bbox.height || 0) / 2;
    const xPct = Math.min(100, Math.max(0, (centerX / width) * 100));
    const yPct = Math.min(100, Math.max(0, (centerY / height) * 100));
    return `${xPct}% ${yPct}%`;
};

const PersonAvatarMedia: React.FC<{ rep?: PersonFace; alt: string }> = ({ rep, alt }) => {
    const mediaRef = useRef<HTMLDivElement | null>(null);
    const authEnabled = isAuthEnabled();
    const [shouldLoad, setShouldLoad] = useState(!authEnabled);
    const [cropUrl, setCropUrl] = useState('');
    const [fallbackUrl, setFallbackUrl] = useState('');
    const filename = rep?.filename;
    const fallbackPath = getFaceFallbackPath(filename);
    const proxyFallbackPath = getFaceProxyThumbnailPath(filename);

    useEffect(() => {
        if (!authEnabled || shouldLoad) {
            return undefined;
        }
        const node = mediaRef.current;
        if (!node || typeof IntersectionObserver === 'undefined') {
            setShouldLoad(true);
            return undefined;
        }
        const observer = new IntersectionObserver((entries) => {
            if (entries.some((entry) => entry.isIntersecting)) {
                setShouldLoad(true);
                observer.disconnect();
            }
        }, { rootMargin: '360px 0px' });
        observer.observe(node);
        return () => observer.disconnect();
    }, [authEnabled, shouldLoad]);

    useEffect(() => {
        let active = true;
        if (!shouldLoad || !filename) {
            setCropUrl('');
            setFallbackUrl('');
            return undefined;
        }
        void (async () => {
            if (rep?.faceId) {
                try {
                    const result = await getUploadJson(`/api/faces/crop/${encodeURIComponent(rep.faceId)}`);
                    if (active && typeof result?.url === 'string') {
                        setCropUrl(resolveApiUrl(result.url));
                    }
                } catch {
                    if (active) {
                        setCropUrl('');
                    }
                }
            }
            try {
                const result = await getUploadJson(`/api/photos/access/thumbnail/${encodeURIComponent(filename)}`);
                if (active && typeof result?.url === 'string') {
                    setFallbackUrl(result.url);
                } else if (active) {
                    setFallbackUrl(proxyFallbackPath || '');
                }
            } catch {
                if (active) {
                    setFallbackUrl(proxyFallbackPath || '');
                }
            }
        })();
        return () => {
            active = false;
        };
    }, [filename, proxyFallbackPath, rep?.faceId, shouldLoad]);

    const imageUrl = cropUrl || (fallbackPath ? fallbackUrl : proxyFallbackPath);

    return (
        <div ref={mediaRef} className="person-avatar-media">
            {shouldLoad && imageUrl ? (
                <img
                    src={imageUrl}
                    alt={alt}
                    loading="lazy"
                    style={{ objectPosition: getFaceObjectPosition(rep) }}
                    onError={(e) => {
                        if (fallbackUrl && e.currentTarget.src !== fallbackUrl) {
                            e.currentTarget.src = fallbackUrl;
                        }
                    }}
                />
            ) : (
                <div className="person-avatar-loading">Loading...</div>
            )}
        </div>
    );
};

const FaceClusters: React.FC = () => {
    const [initialLoading, setInitialLoading] = useState(false);
    const [actionLoading, setActionLoading] = useState(false);
    const [persons, setPersons] = useState<PersonSummary[]>([]);
    const [searchQuery, setSearchQuery] = useState<string>('');
    const [selected, setSelected] = useState<Record<string, boolean>>({});
    const [mergeTarget, setMergeTarget] = useState<string | null>(null);
    const [lastMergeId, setLastMergeId] = useState<string | null>(null);
    const [status, setStatus] = useState<string>('');
    const [merges, setMerges] = useState<MergeHistoryItem[]>([]);
    const [suggestions, setSuggestions] = useState<SuggestionItem[]>([]);
    const navigate = useNavigate();
    const busy = initialLoading || actionLoading;
    const formatMergeId = (mergeId: string) => {
        if (!mergeId) return '';
        return mergeId.length > 12 ? `${mergeId.slice(0, 6)}…${mergeId.slice(-4)}` : mergeId;
    };
    const getPersonLabel = (personId?: string | null) => {
        if (!personId) return 'Unknown person';
        const match = persons.find((p) => p.personId === personId);
        if (match?.name) return match.name;
        return `Person ${formatMergeId(personId)}`;
    };
    const getHistoryLabel = (personId?: string | null) => {
        if (!personId) return 'Unknown person';
        const match = persons.find((p) => p.personId === personId);
        return match?.name || 'Unknown person';
    };
    const formatMergeSummary = (merge?: MergeHistoryItem) => {
        if (!merge) return 'Merge completed';
        const targetLabel = merge?.targetName || getHistoryLabel(merge?.targetPersonId);
        const mergedNames = Array.isArray(merge?.mergedNames) ? merge.mergedNames.filter(Boolean) : [];
        const mergedLabels = mergedNames.length > 0
            ? mergedNames
            : (Array.isArray(merge?.mergedIds) ? merge.mergedIds.map((id: string) => getHistoryLabel(id)) : []).filter(Boolean);
        if (mergedLabels.length === 0) return `Merged into ${targetLabel}`;
        return `Merged ${mergedLabels.join(', ')} → ${targetLabel}`;
    };

    const selectedCount = useMemo(() => Object.values(selected).filter(Boolean).length, [selected]);
    const selectedPersonIds = useMemo(() => Object.keys(selected).filter(id => selected[id]), [selected]);

    const getInitials = (name?: string | null, personId?: string | null) => {
        if (name) {
            const parts = name.split(' ').filter(Boolean).slice(0, 2);
            return parts.map((part) => part[0]?.toUpperCase()).join('') || 'P';
        }
        if (personId) {
            return personId.slice(0, 2).toUpperCase();
        }
        return 'P';
    };

    const hashCode = (value: string) => {
        let hash = 0;
        for (let i = 0; i < value.length; i += 1) {
            hash = (hash << 5) - hash + value.charCodeAt(i);
            hash |= 0;
        }
        return Math.abs(hash);
    };

    const getAvatarStyle = (personId?: string | null) => {
        const seed = personId ? hashCode(personId) : 0;
        const hue = seed % 360;
        const hue2 = (hue + 36) % 360;
        return {
            backgroundImage: `radial-gradient(120% 120% at 30% 20%, hsla(${hue}, 70%, 86%, 0.95) 0%, hsla(${hue2}, 60%, 72%, 0.85) 45%, hsla(${hue}, 50%, 60%, 0.6) 100%)`
        };
    };

    const loadPersons = async (q?: string, showSpinner = true) => {
        if (showSpinner) {
            setInitialLoading(true);
        }
        try {
            const res = await faceService.listPersons(q || '');
            setPersons((res.persons || []) as PersonSummary[]);
        } catch (e: unknown) {
            setStatus(String(e));
        } finally {
            if (showSpinner) {
                setInitialLoading(false);
            }
        }
    };

    const refreshSupportData = async () => {
        try {
            const [m, s] = await Promise.all([
                faceService.listMerges(),
                faceService.listSuggestions(),
            ]);
            setMerges((m.merges || []) as MergeHistoryItem[]);
            setSuggestions((s.suggestions || []) as SuggestionItem[]);
        } catch (e: unknown) {
            setStatus(String(e));
        }
    };

    const getSuggestionPersonId = (value?: string) => value || '';

    const removePersonsFromState = (personIds: string[]) => {
        const removeSet = new Set(personIds);
        setPersons(prev => prev.filter(person => !removeSet.has(person.personId)));
        setSelected(prev => {
            const next = { ...prev };
            personIds.forEach(personId => delete next[personId]);
            return next;
        });
        setSuggestions(prev => prev.filter((suggestion) => (
            !removeSet.has(getSuggestionPersonId(suggestion.sourcePersonId)) && !removeSet.has(getSuggestionPersonId(suggestion.targetPersonId))
        )));
        if (mergeTarget && removeSet.has(mergeTarget)) {
            setMergeTarget(null);
        }
    };

    const applyMergeToState = (targetId: string, mergeIds: string[]) => {
        const mergeSet = new Set(mergeIds);
        setPersons(prev => {
            const target = prev.find(person => person.personId === targetId);
            const merged = prev.filter(person => mergeSet.has(person.personId));
            if (!target) {
                return prev.filter(person => !mergeSet.has(person.personId));
            }
            const mergedFaceIds = merged.flatMap(person => person.faceIds || []);
            const faceIds = Array.from(new Set([...(target.faceIds || []), ...mergedFaceIds]));
            const representativeFace = target.representativeFace || merged.find(person => person.representativeFace)?.representativeFace;
            return prev
                .filter(person => !mergeSet.has(person.personId))
                .map(person => person.personId === targetId
                    ? { ...person, faceIds, faceCount: faceIds.length, representativeFace }
                    : person);
        });
        setSelected({});
        setMergeTarget(null);
        setSuggestions(prev => prev.filter((suggestion) => (
            suggestion.sourcePersonId !== targetId
            && suggestion.targetPersonId !== targetId
            && !mergeSet.has(getSuggestionPersonId(suggestion.sourcePersonId))
            && !mergeSet.has(getSuggestionPersonId(suggestion.targetPersonId))
        )));
    };

    useEffect(() => {
        void (async () => {
            await loadPersons();
            void refreshSupportData();
        })();
    }, []);
    const handleSearch = async () => {
        setStatus('Searching...');
        await loadPersons(searchQuery.trim());
        void refreshSupportData();
    };

    const handleAssignUnclustered = async () => {
        setStatus('Assigning unclustered faces...');
        setActionLoading(true);
        try {
            const response = await faceService.assignUnclusteredFaces();
            if (response?.queued) {
                const jobId = String(response?.jobId || 'pending');
                setStatus(`Queued clustering job ${jobId}.`);
                showToast('Queued clustering job');
            } else {
                const assigned = Number(response?.assignedFaces || 0);
                const created = Number(response?.createdPeople || 0);
                setStatus(`Assigned ${assigned} unclustered face${assigned === 1 ? '' : 's'}; created ${created} people.`);
                showToast(`Assigned ${assigned} unclustered face${assigned === 1 ? '' : 's'}`);
            }
            await loadPersons(searchQuery.trim(), false);
            void refreshSupportData();
        } catch (e: unknown) {
            setStatus(String(e));
            showToast(String(e));
        } finally {
            setActionLoading(false);
        }
    };

    const handleMergeSuggestion = async (targetId: string, sourceId: string, label: string) => {
        if (!targetId || !sourceId) return;
        if (!window.confirm(`Merge ${label} into ${getPersonLabel(targetId)}?`)) return;
        setActionLoading(true);
        try {
            const res = await faceService.mergePersons(targetId, [sourceId]);
            setLastMergeId(res.mergeId || null);
            applyMergeToState(targetId, [sourceId]);
            showToast('Merge completed');
            void refreshSupportData();
        } catch (e: any) {
            setStatus(String(e));
            showToast(String(e));
        } finally {
            setActionLoading(false);
        }
    };

    const handleDeletePerson = async (personId: string, label: string) => {
        if (!window.confirm(`Delete cluster ${label}? Photos and detected faces will stay, but this person assignment will be removed.`)) return;
        setActionLoading(true);
        setStatus('Deleting cluster...');
        try {
            await faceService.deletePerson(personId);
            removePersonsFromState([personId]);
            showToast('Cluster deleted');
            setStatus('Cluster deleted.');
            void refreshSupportData();
        } catch (e: any) {
            setStatus(String(e));
            showToast(String(e));
        } finally {
            setActionLoading(false);
        }
    };

    const selectAllVisible = () => {
        setSelected(persons.reduce<Record<string, boolean>>((next, person) => {
            if (person.personId) {
                next[person.personId] = true;
            }
            return next;
        }, {}));
    };

    const clearSelected = () => {
        setSelected({});
        setMergeTarget(null);
    };

    const handleDeleteSelected = async () => {
        const personIds = selectedPersonIds;
        if (personIds.length === 0) {
            setStatus('Select at least one cluster to delete.');
            return;
        }
        if (!window.confirm(`Delete ${personIds.length} selected cluster${personIds.length === 1 ? '' : 's'}? Photos and detected faces will stay, but these person assignments will be removed.`)) {
            return;
        }
        setActionLoading(true);
        setStatus(`Deleting ${personIds.length} cluster${personIds.length === 1 ? '' : 's'}...`);
        showToast('Deleting selected clusters...');
        try {
            const result = await faceService.deletePersons(personIds);
            const deletedCount = result.deletedPersonIds.length;
            const errorCount = result.errors.length;
            removePersonsFromState(result.deletedPersonIds);
            setStatus(`Deleted ${deletedCount} cluster${deletedCount === 1 ? '' : 's'}${errorCount ? `, ${errorCount} failed` : ''}.`);
            showToast(errorCount ? `Deleted ${deletedCount}, ${errorCount} failed` : 'Selected clusters deleted');
            void refreshSupportData();
        } catch (e: any) {
            setStatus(String(e));
            showToast(String(e));
        } finally {
            setActionLoading(false);
        }
    };

    const handleMergeSelected = async () => {
        const toMerge = Object.keys(selected).filter(id => selected[id]);
        if (!mergeTarget) {
            setStatus('Please select a target person to merge into.');
            return;
        }
        if (toMerge.length <= 1) {
            setStatus('Select at least two persons to merge.');
            return;
        }
        const mergeIds = toMerge.filter(id => id !== mergeTarget);
        if (mergeIds.length === 0) {
            setStatus('No other persons selected to merge.');
            return;
        }
        if (!window.confirm(`Merge ${mergeIds.length} persons into ${getPersonLabel(mergeTarget)}? This cannot be easily undone.`)) {
            return;
        }
        setStatus('Merging...');
        showToast('Merging profiles...');
        setActionLoading(true);
        try {
            const res = await faceService.mergePersons(mergeTarget, mergeIds);
            setStatus('Merge completed. You can undo the merge.');
            showToast('Merge completed');
            setLastMergeId(res.mergeId || null);
            applyMergeToState(mergeTarget, mergeIds);
            void refreshSupportData();
        } catch (e: any) {
            setStatus(String(e));
            showToast(String(e));
        } finally {
            setActionLoading(false);
        }
    };

    const handleUndoMerge = async (mergeId: string) => {
        if (!mergeId || !window.confirm('Undo this merge?')) return;
        setStatus('Undoing merge...');
        showToast('Undoing merge');
        setActionLoading(true);
        try {
            await faceService.undoMerge(mergeId);
            showToast('Merge undone');
            setStatus('Merge undone.');
            if (mergeId === lastMergeId) {
                setLastMergeId(null);
            }
            await loadPersons(searchQuery.trim(), false);
            void refreshSupportData();
        } catch (e: any) {
            setStatus(String(e));
            showToast(String(e));
        } finally {
            setActionLoading(false);
        }
    };

    return (
        <section className="card-glass face-clusters people-minimal">
            <header className="people-hero">
                <div className="people-hero-left">
                    <div className="people-hero-icon" aria-hidden="true">
                        <UserCircleIcon />
                    </div>
                    <div>
                        <p className="people-hero-kicker">People</p>
                        <div className="people-hero-meta">
                            <span className="people-hero-count">{persons.length}</span>
                            <span className="people-hero-label">clusters</span>
                        </div>
                    </div>
                </div>
                <div className="people-hero-actions">
                    <div className="people-searchbar">
                        <MagnifyingGlassIcon className="people-search-icon" />
                        <input
                            type="text"
                            placeholder="Search"
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter') { void handleSearch(); } }}
                        />
                        {searchQuery && (
                            <button
                                className="people-icon-btn"
                                aria-label="Clear search"
                                onClick={() => { setSearchQuery(''); void loadPersons(); void refreshSupportData(); }}
                                type="button"
                            >
                                <XMarkIcon />
                            </button>
                        )}
                    </div>
                    <button className="people-icon-btn" aria-label="Search" onClick={() => void handleSearch()} disabled={busy} type="button">
                        <MagnifyingGlassIcon />
                    </button>
                    <button className="people-icon-btn" aria-label="Assign unclustered faces" title="Assign unclustered faces" onClick={handleAssignUnclustered} disabled={busy} type="button">
                        <SparklesIcon />
                    </button>
                </div>
            </header>

            {status && <p className="status people-status">{status}</p>}

            {persons.length > 0 && (
                <div className="people-merge-strip">
                    <div className="people-merge-count">{selectedCount} selected</div>
                    <button
                        className="people-secondary-btn"
                        disabled={busy || persons.length === 0 || selectedCount === persons.length}
                        onClick={selectAllVisible}
                        type="button"
                    >
                        <CheckIcon />
                        <span>Select all</span>
                    </button>
                    <button
                        className="people-secondary-btn"
                        disabled={busy || selectedCount === 0}
                        onClick={clearSelected}
                        type="button"
                    >
                        <XMarkIcon />
                        <span>Clear</span>
                    </button>
                    <div className="people-merge-input">
                        <ArrowsRightLeftIcon />
                        <select value={mergeTarget || ''} onChange={(e) => setMergeTarget(e.target.value)}>
                            <option value="">Target</option>
                            {persons.map(p => (
                                <option key={p.personId} value={p.personId}>{p.name || p.personId}</option>
                            ))}
                        </select>
                    </div>
                    <button
                        className="people-merge-btn"
                        disabled={busy}
                        aria-label="Merge selected"
                        onClick={() => void handleMergeSelected()}
                        type="button"
                    >
                        <ArrowsRightLeftIcon />
                        <span className="sr-only">Merge selected</span>
                    </button>
                    <button
                        className="people-merge-btn people-delete-selected"
                        disabled={busy || selectedCount === 0}
                        aria-label="Delete selected clusters"
                        onClick={() => void handleDeleteSelected()}
                        type="button"
                    >
                        <TrashIcon />
                        <span>Delete selected</span>
                    </button>
                </div>
            )}

            <div className="people-grid">
                {initialLoading && persons.length === 0 && <div className="people-empty">Loading...</div>}
                {!initialLoading && persons.length === 0 && <div className="people-empty">No people yet.</div>}
                {persons.map((p, index: number) => {
                    const suspiciousRepresentative = isSuspiciousFace(p.representativeFace);
                    return (
                        <div key={p.personId} className={`person-tile ${selected[p.personId] ? 'is-selected' : ''} ${suspiciousRepresentative ? 'has-suspicious-face' : ''}`} style={{ ['--stagger' as string]: `${Math.min(index, 18) * 24}ms` }}>
                            <button className="person-tile-main" onClick={() => navigate(`/people/${p.personId}`)} type="button">
                                <div className="person-avatar" style={getAvatarStyle(p.personId)}>
                                    {p.representativeFace?.filename ? (
                                        <PersonAvatarMedia
                                            rep={p.representativeFace}
                                            alt={p.name || 'Person'}
                                        />
                                    ) : (
                                        <span>{getInitials(p.name, p.personId)}</span>
                                    )}
                                    {suspiciousRepresentative && (
                                        <span className="person-avatar-review-badge" title="Needs review" aria-label="Needs review">
                                            <ExclamationTriangleIcon />
                                        </span>
                                    )}
                                </div>
                            </button>
                            <label className="person-select">
                                <input
                                    type="checkbox"
                                    checked={!!selected[p.personId]}
                                    onChange={(e) => setSelected(prev => ({ ...prev, [p.personId]: e.target.checked }))}
                                />
                                <span className="person-select-indicator">
                                    <CheckIcon />
                                </span>
                            </label>
                            <div className="person-meta">
                                <div className="person-name">{p.name || 'Unnamed'}</div>
                                <div className="person-count">{p.faceCount ?? (p.faceIds || []).length}</div>
                            </div>
                            <button className="person-open" aria-label="Open person" onClick={() => navigate(`/people/${p.personId}`)} type="button">
                                <ArrowRightIcon />
                            </button>
                            <button
                                className="person-open person-delete"
                                aria-label="Delete cluster"
                                onClick={() => void handleDeletePerson(p.personId, p.name || 'Unnamed')}
                                type="button"
                                disabled={busy}
                            >
                                <TrashIcon />
                            </button>
                        </div>
                    );
                })}
            </div>

            {suggestions.length > 0 && (
                <div className="people-panel people-suggestions">
                    <div className="people-panel-header">
                        <div className="people-panel-title">Suggestions</div>
                        <div className="people-panel-meta">Potential duplicates</div>
                    </div>
                    <div className="people-suggestion-list">
                        {suggestions.map((s) => {
                            const sourceLabel = getPersonLabel(s.sourcePersonId);
                            const targetLabel = getPersonLabel(s.targetPersonId);
                            const score = typeof s.similarity === 'number' ? s.similarity : 0;
                            return (
                                <div key={`${s.sourcePersonId}-${s.targetPersonId}`} className="people-suggestion-row">
                                    <div className="people-suggestion-main">
                                        <div className="people-suggestion-title">
                                            <span className="people-suggestion-name">{sourceLabel}</span>
                                            <span className="people-suggestion-arrow">→</span>
                                            <span className="people-suggestion-name">{targetLabel}</span>
                                        </div>
                                        <div className="people-suggestion-meta">
                                            <span className="people-merge-chip">{s.sourceFaceCount || 0}</span>
                                            <span className="people-merge-chip">{(score * 100).toFixed(1)}%</span>
                                        </div>
                                    </div>
                                    <div className="people-suggestion-actions">
                                        <button
                                            className="people-merge-btn"
                                            disabled={busy}
                                onClick={() => handleMergeSuggestion(s.targetPersonId || '', s.sourcePersonId || '', sourceLabel)}
                                            type="button"
                                            aria-label="Merge suggestion"
                                        >
                                            <ArrowsRightLeftIcon />
                                            <span className="sr-only">Merge suggestion</span>
                                        </button>
                                        <button className="people-icon-btn" onClick={() => s.targetPersonId && navigate(`/people/${s.targetPersonId}`)} aria-label="Review suggestion" type="button">
                                            <ArrowRightIcon />
                                        </button>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>
            )}

            {(lastMergeId || merges.length > 0) && (
                <div className="people-panel people-history">
                    <div className="people-panel-header">
                        <div className="people-panel-title">History</div>
                        <div className="people-panel-meta">Undo merges</div>
                    </div>
                    {lastMergeId && (
                        <div className="people-merge-latest">
                            <div className="people-merge-history-main">
                                <div className="people-merge-history-title">
                                    <span className="people-merge-chip">
                                        {formatMergeSummary(merges.find((m) => m.mergeId === lastMergeId))}
                                    </span>
                                </div>
                            </div>
                            <button className="people-merge-btn" disabled={busy} aria-label="Undo last merge" onClick={() => lastMergeId && void handleUndoMerge(lastMergeId)} type="button">
                                <XMarkIcon />
                                <span className="sr-only">Undo last merge</span>
                            </button>
                        </div>
                    )}
                    <div className="people-merge-history-list">
                        {merges.slice(0, 5).map((m) => (
                            <div key={m.mergeId} className="people-merge-history-row">
                                <div className="people-merge-history-main">
                                    <div className="people-merge-history-title">
                                        <span className="people-merge-chip">{formatMergeSummary(m)}</span>
                                        <span className="people-merge-chip">{m.createdAt}</span>
                                    </div>
                                </div>
                                <div className="people-merge-history-actions">
                                    <button className="people-icon-btn" disabled={busy} onClick={() => m.mergeId && void handleUndoMerge(m.mergeId)} aria-label="Undo merge" type="button">
                                        <XMarkIcon />
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </section>
    );
};

export default FaceClusters;

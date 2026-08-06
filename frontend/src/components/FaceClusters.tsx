import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { EmptyState } from './shared/EmptyState';
import { confirmDialog } from './shared/dialogs';
import { Loading } from './shared/Loading';
import { ArrowsRightLeftIcon, ArrowRightIcon, CheckIcon, ChevronLeftIcon, ChevronRightIcon, ExclamationTriangleIcon, FaceSmileIcon, MagnifyingGlassIcon, NoSymbolIcon, SparklesIcon, Squares2X2Icon, TrashIcon, UserCircleIcon, UsersIcon, XMarkIcon } from '@heroicons/react/24/outline';
import faceService from '../services/faceService';
import { resolveFaceCropUrl, resolveFaceFallbackUrl } from '../services/faceMediaCache';
import { showToast } from '../services/toast';
import { requestJobPoll } from '../services/jobNotifications';
import { notifyApiError } from '../services/requestFeedback';
import { isAuthEnabled } from '../services/authClient';
import { useAppServices } from './AppServicesProvider';
import { useNavigate } from 'react-router-dom';
import type { FlatFace, MergeHistoryItem, PersonFace, PersonSummary, SuggestionItem } from '../types/people';
import { compareNamedFirstThenAlpha, isNamedPerson } from '../utils/people';

const SUSPICIOUS_FACE_CONFIDENCE = 0.6;
const CLUSTER_PER_PAGE = 15;
const FACES_PER_PAGE = 50;
const SUGGESTIONS_PER_PAGE = 5;
type PeopleView = 'cluster' | 'face';
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
    const wrapperRef = useRef<HTMLDivElement | null>(null);
    const mountedRef = useRef(true);
    const fallbackRequestedRef = useRef(false);
    const authEnabled = isAuthEnabled();
    const [shouldLoad, setShouldLoad] = useState(!authEnabled);
    const [cropUrl, setCropUrl] = useState('');
    const [cropErrored, setCropErrored] = useState(false);
    const [fallbackUrl, setFallbackUrl] = useState('');
    const filename = rep?.filename;
    const faceId = rep?.faceId;
    const fallbackPath = getFaceFallbackPath(filename);
    const proxyFallbackPath = getFaceProxyThumbnailPath(filename);
    // Prefer the direct-blob SAS thumbnail the listing already provided
    // (rep.thumbnailUrl) over a per-face access round trip; only hit the
    // backend when it's absent (RAW/HEIC or non-SAS mode).
    const inlineThumb = typeof rep?.thumbnailUrl === 'string' && /^https?:\/\//i.test(rep.thumbnailUrl)
        ? rep.thumbnailUrl
        : '';

    useEffect(() => {
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
        };
    }, []);

    useEffect(() => {
        if (!authEnabled || shouldLoad) {
            return undefined;
        }
        const node = wrapperRef.current;
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

    // Only fetched when the crop is unavailable/fails — previously this ran
    // unconditionally alongside the crop fetch, doubling request volume for
    // every avatar even when the crop loaded fine.
    const ensureFallback = useCallback(() => {
        if (fallbackRequestedRef.current || !filename) {
            return;
        }
        fallbackRequestedRef.current = true;
        void resolveFaceFallbackUrl(filename, inlineThumb, proxyFallbackPath).then((url) => {
            if (mountedRef.current) {
                setFallbackUrl(url);
            }
        });
    }, [filename, inlineThumb, proxyFallbackPath]);

    useEffect(() => {
        fallbackRequestedRef.current = false;
        setCropUrl('');
        setCropErrored(false);
        setFallbackUrl('');
        if (!shouldLoad || !filename) {
            return undefined;
        }
        if (!faceId) {
            ensureFallback();
            return undefined;
        }
        let active = true;
        resolveFaceCropUrl(faceId)
            .then((url) => {
                if (active) {
                    setCropUrl(url);
                }
            })
            .catch(() => {
                if (active) {
                    ensureFallback();
                }
            });
        return () => {
            active = false;
        };
    }, [shouldLoad, faceId, filename, ensureFallback]);

    // Reactive to cropErrored, so once the crop <img> fails this falls through
    // to fallbackUrl (or the loading placeholder while it's still resolving)
    // on the next render — no imperative DOM src swap needed.
    const imageUrl = (!cropErrored && cropUrl) || fallbackUrl || (fallbackPath ? '' : proxyFallbackPath);

    return (
        <div ref={wrapperRef} className="person-avatar-media">
            {shouldLoad && imageUrl ? (
                <img
                    src={imageUrl}
                    alt={alt}
                    loading="lazy"
                    style={{ objectPosition: getFaceObjectPosition(rep) }}
                    onError={() => {
                        setCropErrored(true);
                        ensureFallback();
                    }}
                />
            ) : (
                <div className="person-avatar-loading">Loading…</div>
            )}
        </div>
    );
};

const FaceClusters: React.FC = () => {
    const { clusteringActive, clusteringStatusLabel } = useAppServices();
    const [initialLoading, setInitialLoading] = useState(false);
    const [actionLoading, setActionLoading] = useState(false);
    const [persons, setPersons] = useState<PersonSummary[]>([]);
    const [searchQuery, setSearchQuery] = useState<string>('');
    const [searchOpen, setSearchOpen] = useState<boolean>(false);
    const searchRef = useRef<HTMLDivElement | null>(null);
    const [selected, setSelected] = useState<Record<string, boolean>>({});
    const [mergeTarget, setMergeTarget] = useState<string | null>(null);
    const [lastMergeId, setLastMergeId] = useState<string | null>(null);
    const [status, setStatus] = useState<string>('');
    const [merges, setMerges] = useState<MergeHistoryItem[]>([]);
    const [suggestions, setSuggestions] = useState<SuggestionItem[]>([]);
    const [view, setView] = useState<PeopleView>('cluster');
    const [page, setPage] = useState(1);
    const [suggestionPage, setSuggestionPage] = useState(1);
    const [faces, setFaces] = useState<FlatFace[]>([]);
    const [facesLoaded, setFacesLoaded] = useState(false);
    const [facesLoading, setFacesLoading] = useState(false);
    const [selectedFaces, setSelectedFaces] = useState<Record<string, boolean>>({});
    // Multi-select for the Suggestions panel, keyed by `${sourceId}::${targetId}`.
    const [selectedSuggestions, setSelectedSuggestions] = useState<Record<string, boolean>>({});
    const navigate = useNavigate();
    const busy = initialLoading || actionLoading;

    useEffect(() => {
        if (!searchOpen) {
            return;
        }
        const handlePointerDown = (event: PointerEvent) => {
            if (searchRef.current && !searchRef.current.contains(event.target as Node)) {
                setSearchOpen(false);
            }
        };
        document.addEventListener('pointerdown', handlePointerDown);
        return () => document.removeEventListener('pointerdown', handlePointerDown);
    }, [searchOpen]);

    // Face view shows a flat, paginated grid of individual face crops. Filter by
    // person name so the shared search box narrows faces the same way it narrows
    // clusters, then page the result 50 at a time.
    const visibleFaces = useMemo(() => {
        const q = searchQuery.trim().toLowerCase();
        if (!q) return faces;
        return faces.filter((face) => (face.personName || '').toLowerCase().includes(q));
    }, [faces, searchQuery]);

    // Named clusters first (kept together), then unnamed — stable within each group
    // so the backend ordering is preserved. Drives both the paginated grid and the
    // merge target dropdown.
    const sortedPersons = useMemo(
        () => [...persons].sort((a, b) => Number(isNamedPerson(b)) - Number(isNamedPerson(a))),
        [persons],
    );
    const activeItemCount = view === 'face' ? visibleFaces.length : sortedPersons.length;
    const activePerPage = view === 'face' ? FACES_PER_PAGE : CLUSTER_PER_PAGE;
    const totalPages = Math.max(1, Math.ceil(activeItemCount / activePerPage));
    const pagedPersons = useMemo(
        () => sortedPersons.slice((page - 1) * CLUSTER_PER_PAGE, page * CLUSTER_PER_PAGE),
        [sortedPersons, page],
    );
    const pagedFaces = useMemo(
        () => visibleFaces.slice((page - 1) * FACES_PER_PAGE, page * FACES_PER_PAGE),
        [visibleFaces, page],
    );
    // Suggestions whose merge target is a named cluster first, then unnamed
    // targets — alphabetical by target name within each group.
    const sortedSuggestions = useMemo(() => {
        const targetNameById = new Map(persons.map((p) => [p.personId, p.name]));
        return [...suggestions].sort((a, b) => compareNamedFirstThenAlpha(
            targetNameById.get(a.targetPersonId || '') ?? a.targetName,
            targetNameById.get(b.targetPersonId || '') ?? b.targetName,
        ));
    }, [suggestions, persons]);
    const suggestionTotalPages = Math.max(1, Math.ceil(sortedSuggestions.length / SUGGESTIONS_PER_PAGE));
    const pagedSuggestions = useMemo(
        () => sortedSuggestions.slice((suggestionPage - 1) * SUGGESTIONS_PER_PAGE, suggestionPage * SUGGESTIONS_PER_PAGE),
        [sortedSuggestions, suggestionPage],
    );
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
    const selectedFaceCount = useMemo(() => Object.values(selectedFaces).filter(Boolean).length, [selectedFaces]);
    const selectedFaceIds = useMemo(() => Object.keys(selectedFaces).filter(id => selectedFaces[id]), [selectedFaces]);

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
            // Person membership may have changed; let the face grid refetch lazily
            // the next time it is shown (or immediately if it is already open).
            setFacesLoaded(false);
        } catch (e: unknown) {
            setStatus(String(e));
            notifyApiError(e, { context: "Couldn't load people", retry: () => { void loadPersons(q, showSpinner); } });
        } finally {
            if (showSpinner) {
                setInitialLoading(false);
            }
        }
    };

    const loadFaces = async () => {
        setFacesLoading(true);
        try {
            const res = await faceService.listFaces();
            const nextFaces = (res.faces || []) as FlatFace[];
            setFaces(nextFaces);
            setFacesLoaded(true);
            // Drop selections for faces that no longer exist.
            const liveIds = new Set(nextFaces.map((face) => face.faceId || ''));
            setSelectedFaces((prev) => {
                const next: Record<string, boolean> = {};
                Object.keys(prev).forEach((id) => {
                    if (prev[id] && liveIds.has(id)) {
                        next[id] = true;
                    }
                });
                return next;
            });
        } catch (e: unknown) {
            setStatus(String(e));
            notifyApiError(e, { context: "Couldn't load faces", retry: () => { void loadFaces(); } });
        } finally {
            setFacesLoading(false);
        }
    };

    const refreshSupportData = async () => {
        try {
            const [m, s] = await Promise.all([
                faceService.listMerges(),
                faceService.listSuggestions(),
            ]);
            setMerges((m.merges || []) as MergeHistoryItem[]);
            const rawSuggestions = (s.suggestions || []) as SuggestionItem[];
            setSuggestions(rawSuggestions);
        } catch (e: unknown) {
            setStatus(String(e));
            notifyApiError(e, { context: "Couldn't load merge history / suggestions", retry: () => { void refreshSupportData(); } });
        }
    };

    const getSuggestionPersonId = (value?: string) => value || '';

    const getSuggestionFace = (suggestion: SuggestionItem, side: 'source' | 'target'): PersonFace | undefined => {
        const direct = side === 'source' ? suggestion.sourceFace : suggestion.targetFace;
        if (direct?.filename) {
            return direct;
        }
        const personId = side === 'source' ? suggestion.sourcePersonId : suggestion.targetPersonId;
        return persons.find((p) => p.personId === personId)?.representativeFace;
    };

    const suggestionKey = (suggestion: SuggestionItem) => (
        `${getSuggestionPersonId(suggestion.sourcePersonId)}::${getSuggestionPersonId(suggestion.targetPersonId)}`
    );

    const toggleSuggestion = (key: string) => {
        setSelectedSuggestions((prev) => ({ ...prev, [key]: !prev[key] }));
    };

    const deselectSuggestion = (key: string) => {
        setSelectedSuggestions((prev) => {
            if (!prev[key]) return prev;
            const next = { ...prev };
            delete next[key];
            return next;
        });
    };

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

    // Keep the current page within range as the person list shrinks (deletes/merges/search).
    useEffect(() => {
        setPage((prev) => Math.min(prev, totalPages));
    }, [totalPages]);

    // Keep the suggestion page within range as suggestions are declined/merged away.
    useEffect(() => {
        setSuggestionPage((prev) => Math.min(prev, suggestionTotalPages));
    }, [suggestionTotalPages]);

    // Lazily fetch the flat face list the first time the Faces view is opened (or
    // after a cluster change invalidates it) so switching tabs stays instant.
    useEffect(() => {
        if (view !== 'face' || facesLoaded || facesLoading) {
            return;
        }
        void loadFaces();
    }, [view, facesLoaded, facesLoading]);

    const handleSearch = async () => {
        setStatus('Searching…');
        setPage(1);
        await loadPersons(searchQuery.trim());
        void refreshSupportData();
    };

    const toggleFace = (faceId: string) => {
        if (!faceId) return;
        setSelectedFaces((prev) => ({ ...prev, [faceId]: !prev[faceId] }));
    };

    // Select every face on the current page. Page-scoped (rather than the whole
    // filtered set) keeps a bulk delete predictable when there are many faces.
    const selectAllVisibleFaces = () => {
        setSelectedFaces((prev) => {
            const next = { ...prev };
            pagedFaces.forEach((face) => {
                if (face.faceId) {
                    next[face.faceId] = true;
                }
            });
            return next;
        });
    };

    const clearSelectedFaces = () => setSelectedFaces({});

    const performFaceDelete = async (faceIds: string[], label: string) => {
        const ids = faceIds.filter(Boolean);
        if (ids.length === 0) {
            return;
        }
        if (!(await confirmDialog({
            title: ids.length === 1 ? 'Delete face' : 'Delete faces',
            message: `Delete ${label}? The photo${ids.length === 1 ? '' : 's'} will stay, but ${ids.length === 1 ? 'this face' : 'these faces'} will be removed from People.`,
            confirmLabel: 'Delete',
            danger: true,
        }))) {
            return;
        }
        setActionLoading(true);
        setStatus(`Deleting ${ids.length} face${ids.length === 1 ? '' : 's'}…`);
        try {
            const result = await faceService.deleteFaces(ids);
            const deletedSet = new Set(result.deleted);
            setFaces((prev) => prev.filter((face) => !deletedSet.has(face.faceId || '')));
            setSelectedFaces((prev) => {
                const next = { ...prev };
                result.deleted.forEach((id) => delete next[id]);
                return next;
            });
            if (result.deletedPersonIds.length > 0) {
                removePersonsFromState(result.deletedPersonIds);
            }
            const failed = result.errors.length;
            setStatus(`Deleted ${result.deleted.length} face${result.deleted.length === 1 ? '' : 's'}${failed ? `, ${failed} failed` : ''}.`);
            showToast(failed ? `Deleted ${result.deleted.length}, ${failed} failed` : 'Faces deleted');
            // Person face counts changed; refresh clusters and suggestions quietly.
            void loadPersons(searchQuery.trim(), false);
            void refreshSupportData();
        } catch (e: unknown) {
            setStatus(String(e));
            notifyApiError(e, { context: 'Couldn’t delete faces.', retry: () => performFaceDelete(faceIds, label) });
        } finally {
            setActionLoading(false);
        }
    };

    const handleDeleteSingleFace = (faceId: string) => performFaceDelete([faceId], 'this face');

    const handleDeleteSelectedFaces = async () => {
        const ids = selectedFaceIds;
        if (ids.length === 0) {
            setStatus('Select at least one face to delete.');
            return;
        }
        await performFaceDelete(ids, `${ids.length} selected face${ids.length === 1 ? '' : 's'}`);
    };

    const handleAssignUnclustered = async () => {
        setStatus('Assigning unclustered faces…');
        setActionLoading(true);
        try {
            const response = await faceService.assignUnclusteredFaces();
            if (response?.queued) {
                const jobId = String(response?.jobId || 'pending');
                setStatus(`Queued clustering job ${jobId}.`);
                showToast('Queued clustering job');
                // Wake the job poller so the in-progress indicator (banner + pill)
                // appears immediately instead of on the next focus/tick.
                requestJobPoll();
            } else {
                const assigned = Number(response?.assignedFaces || 0);
                const created = Number(response?.createdPeople || 0);
                setStatus(`Assigned ${assigned} unclustered face${assigned === 1 ? '' : 's'}; created ${created} people.`);
                showToast(`Assigned ${assigned} unclustered face${assigned === 1 ? '' : 's'}`);
            }
            await loadPersons(searchQuery.trim(), false);
            void refreshSupportData();
        } catch (e: unknown) {
            setStatus('');
            notifyApiError(e, { context: "Couldn't assign unclustered faces", retry: () => { void handleAssignUnclustered(); } });
        } finally {
            setActionLoading(false);
        }
    };

    // After a merge only the History (for Undo) needs re-fetching. The suggestions
    // list is updated optimistically, so we deliberately do NOT refetch it here —
    // that full refetch (refreshSupportData) is what made the whole Suggestions
    // section visibly reload after every merge.
    const refreshMergeHistory = async () => {
        try {
            const m = await faceService.listMerges();
            setMerges((m.merges || []) as MergeHistoryItem[]);
        } catch {
            // History is non-critical; ignore transient refresh failures.
        }
    };

    // Resync suggestions + people from the server. Used only on the rare error
    // path, to roll back an optimistic change the server ended up rejecting.
    const resyncAfterSuggestionError = () => {
        void refreshSupportData();
        void loadPersons(searchQuery.trim(), false);
    };

    // Merge one suggestion. Optimistic and non-blocking: the row disappears and the
    // people list updates immediately, then the network call runs in the background
    // so the user can keep approving other suggestions without waiting. No global
    // spinner and no confirm dialog — a suggestion's Merge button IS the
    // confirmation, and every merge is reversible from History.
    const handleMergeSuggestion = async (targetId: string, sourceId: string) => {
        if (!targetId || !sourceId) return;
        applyMergeToState(targetId, [sourceId]);
        deselectSuggestion(`${sourceId}::${targetId}`);
        try {
            const res = await faceService.mergePersons(targetId, [sourceId]);
            setLastMergeId(res.mergeId || null);
            showToast('Merged');
            void refreshMergeHistory();
        } catch (e: unknown) {
            notifyApiError(e, { context: "Couldn't merge" });
            resyncAfterSuggestionError();
        }
    };

    // Approve every selected suggestion at once. Rows are removed up-front and the
    // merges run in one batched request, so the section stays responsive during a
    // batch — and identity propagation (reclaiming a named person's faces from
    // unnamed clusters) runs as a single background pass afterwards instead of
    // one job per pair, which previously trickled out one completion toast per
    // approval as the worker drained them.
    const handleApproveSelectedSuggestions = async () => {
        const chosen = suggestions.filter((s) => selectedSuggestions[suggestionKey(s)]);
        if (chosen.length === 0) return;
        chosen.forEach((s) => applyMergeToState(s.targetPersonId || '', [s.sourcePersonId || '']));
        setSelectedSuggestions({});
        const res = await faceService.mergePersonsBatch(
            chosen.map((s) => ({ targetPersonId: s.targetPersonId || '', mergeIds: [s.sourcePersonId || ''] })),
        );
        const results = res.results || [];
        const failed = results.filter((r) => !r.success).length;
        const lastOk = [...results].reverse().find((r) => r.success && r.mergeId);
        if (lastOk) setLastMergeId(lastOk.mergeId || null);
        const done = chosen.length - failed;
        showToast(failed ? `Merged ${done}, ${failed} failed` : `Merged ${done} suggestion${done === 1 ? '' : 's'}`);
        if (res.propagateJobId) {
            requestJobPoll();
        }
        void refreshMergeHistory();
        if (failed) resyncAfterSuggestionError();
    };

    const handleDeclineSuggestion = async (sourceId: string, targetId: string) => {
        if (!sourceId || !targetId) return;
        // Optimistic removal keeps the section responsive; resync on failure.
        setSuggestions((prev) => prev.filter((s) => (
            !(getSuggestionPersonId(s.sourcePersonId) === sourceId && getSuggestionPersonId(s.targetPersonId) === targetId)
        )));
        deselectSuggestion(`${sourceId}::${targetId}`);
        try {
            await faceService.declineSuggestion(sourceId, targetId);
            showToast('Suggestion declined');
        } catch (e: unknown) {
            notifyApiError(e, { context: "Couldn't dismiss suggestion" });
            void refreshSupportData();
        }
    };

    const handleDeclineSelectedSuggestions = async () => {
        const chosen = suggestions.filter((s) => selectedSuggestions[suggestionKey(s)]);
        if (chosen.length === 0) return;
        const chosenKeys = new Set(chosen.map((s) => suggestionKey(s)));
        setSuggestions((prev) => prev.filter((s) => !chosenKeys.has(suggestionKey(s))));
        setSelectedSuggestions({});
        const results = await Promise.allSettled(
            chosen.map((s) => faceService.declineSuggestion(s.sourcePersonId || '', s.targetPersonId || '')),
        );
        const failed = results.filter((r) => r.status === 'rejected').length;
        const done = chosen.length - failed;
        showToast(failed ? `Declined ${done}, ${failed} failed` : `Declined ${done} suggestion${done === 1 ? '' : 's'}`);
        if (failed) void refreshSupportData();
    };

    const handleDeletePerson = async (personId: string, label: string) => {
        if (!(await confirmDialog({
            title: 'Delete cluster',
            message: `Delete cluster ${label}? Photos and detected faces will stay, but this person assignment will be removed.`,
            confirmLabel: 'Delete',
            danger: true,
        }))) return;
        setActionLoading(true);
        setStatus('Deleting cluster…');
        try {
            await faceService.deletePerson(personId);
            removePersonsFromState([personId]);
            showToast('Cluster deleted');
            setStatus('Cluster deleted.');
            void refreshSupportData();
        } catch (e: unknown) {
            setStatus('');
            notifyApiError(e, { context: "Couldn't delete cluster", retry: () => { void handleDeletePerson(personId, label); } });
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
        if (!(await confirmDialog({
            title: 'Delete clusters',
            message: `Delete ${personIds.length} selected cluster${personIds.length === 1 ? '' : 's'}? Photos and detected faces will stay, but these person assignments will be removed.`,
            confirmLabel: 'Delete',
            danger: true,
        }))) {
            return;
        }
        setActionLoading(true);
        setStatus(`Deleting ${personIds.length} cluster${personIds.length === 1 ? '' : 's'}…`);
        showToast('Deleting selected clusters…');
        try {
            const result = await faceService.deletePersons(personIds);
            const deletedCount = result.deletedPersonIds.length;
            const errorCount = result.errors.length;
            removePersonsFromState(result.deletedPersonIds);
            setStatus(`Deleted ${deletedCount} cluster${deletedCount === 1 ? '' : 's'}${errorCount ? `, ${errorCount} failed` : ''}.`);
            showToast(errorCount ? `Deleted ${deletedCount}, ${errorCount} failed` : 'Selected clusters deleted');
            void refreshSupportData();
        } catch (e: unknown) {
            setStatus('');
            notifyApiError(e, { context: "Couldn't delete selected clusters", retry: () => { void handleDeleteSelected(); } });
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
        if (!(await confirmDialog({
            title: 'Merge people',
            message: `Merge ${mergeIds.length} persons into ${getPersonLabel(mergeTarget)}? This cannot be easily undone.`,
            confirmLabel: 'Merge',
        }))) {
            return;
        }
        setStatus('Merging…');
        showToast('Merging profiles…');
        setActionLoading(true);
        try {
            const res = await faceService.mergePersons(mergeTarget, mergeIds);
            // Reclaiming matching faces from unnamed clusters now runs in the
            // background (see propagateJobId); tell the user to refresh for it.
            setStatus(res.propagateJobId
                ? 'Merge completed. Finding more matching faces in the background — refresh shortly to see them. You can undo the merge.'
                : 'Merge completed. You can undo the merge.');
            showToast('Merge completed');
            setLastMergeId(res.mergeId || null);
            applyMergeToState(mergeTarget, mergeIds);
            void refreshSupportData();
        } catch (e: unknown) {
            setStatus('');
            notifyApiError(e, { context: "Couldn't merge people", retry: () => { void handleMergeSelected(); } });
        } finally {
            setActionLoading(false);
        }
    };

    const handleUndoMerge = async (mergeId: string) => {
        if (!mergeId) return;
        if (!(await confirmDialog({
            title: 'Undo merge',
            message: 'Undo this merge and restore the separate people?',
            confirmLabel: 'Undo merge',
        }))) return;
        setStatus('Undoing merge…');
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
        } catch (e: unknown) {
            setStatus('');
            notifyApiError(e, { context: "Couldn't undo merge", retry: () => { void handleUndoMerge(mergeId); } });
        } finally {
            setActionLoading(false);
        }
    };

    const selectedSuggestions_list = suggestions.filter((s) => selectedSuggestions[suggestionKey(s)]);
    const selectedSuggestionCount = selectedSuggestions_list.length;
    const allSuggestionsSelected = suggestions.length > 0 && selectedSuggestionCount === suggestions.length;
    const toggleSelectAllSuggestions = () => {
        if (allSuggestionsSelected) {
            setSelectedSuggestions({});
            return;
        }
        const next: Record<string, boolean> = {};
        suggestions.forEach((s) => { next[suggestionKey(s)] = true; });
        setSelectedSuggestions(next);
    };

    return (
        <section className="card-glass face-clusters people-minimal">
            <header className={`people-hero ${searchOpen ? 'is-searching' : ''}`}>
                <div className="people-hero-left">
                    <div className="people-hero-icon" aria-hidden="true">
                        <UserCircleIcon />
                    </div>
                    <div>
                        <p className="people-hero-kicker">People</p>
                        <div className="people-hero-meta">
                            <span className="people-hero-count">{view === 'face' ? faces.length : persons.length}</span>
                            <span className="people-hero-label">{view === 'face' ? 'faces' : 'clusters'}</span>
                        </div>
                    </div>
                </div>
                <div className="people-hero-actions">
                    {searchOpen ? (
                        <div className="people-searchbar" ref={searchRef}>
                            <MagnifyingGlassIcon className="people-search-icon" />
                            <input
                                type="text"
                                placeholder="Search people…"
                                value={searchQuery}
                                autoFocus
                                onChange={(e) => setSearchQuery(e.target.value)}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter') { void handleSearch(); }
                                    else if (e.key === 'Escape') { setSearchOpen(false); }
                                }}
                            />
                            {searchQuery && (
                                <button
                                    className="people-icon-btn"
                                    aria-label="Clear search"
                                    onClick={() => {
                                        setSearchQuery('');
                                        setPage(1);
                                        void loadPersons();
                                        void refreshSupportData();
                                    }}
                                    type="button"
                                >
                                    <XMarkIcon />
                                </button>
                            )}
                        </div>
                    ) : (
                        <button
                            className={`people-icon-btn ${searchQuery ? 'is-active' : ''}`}
                            aria-label="Search"
                            title={searchQuery ? `Searching: ${searchQuery}` : 'Search'}
                            onClick={() => setSearchOpen(true)}
                            type="button"
                        >
                            <MagnifyingGlassIcon />
                        </button>
                    )}
                    <button className="people-icon-btn" aria-label="Assign unclustered faces" title="Assign unclustered faces" onClick={handleAssignUnclustered} disabled={busy} type="button">
                        <SparklesIcon />
                    </button>
                </div>
            </header>

            {clusteringActive && (
                <div className="people-clustering-banner" role="status" aria-live="polite">
                    <span className="bg-activity-pulse" aria-hidden="true" />
                    <span>{clusteringStatusLabel || 'Grouping people…'} This runs in the background — new groups appear when it finishes.</span>
                </div>
            )}

            {status && <p className="status people-status">{status}</p>}

            {persons.length > 0 && (
                <div className="people-view-toggle" role="tablist" aria-label="People view">
                    <button
                        className={`people-view-tab ${view === 'cluster' ? 'is-active' : ''}`}
                        onClick={() => { setView('cluster'); setPage(1); }}
                        role="tab"
                        aria-selected={view === 'cluster'}
                        type="button"
                    >
                        <Squares2X2Icon />
                        <span>Clusters</span>
                    </button>
                    <button
                        className={`people-view-tab ${view === 'face' ? 'is-active' : ''}`}
                        onClick={() => { setView('face'); setPage(1); }}
                        role="tab"
                        aria-selected={view === 'face'}
                        type="button"
                    >
                        <FaceSmileIcon />
                        <span>Faces</span>
                    </button>
                </div>
            )}

            {persons.length > 0 && view === 'cluster' && (
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
                            {sortedPersons.some(isNamedPerson) && (
                                <optgroup label="Named">
                                    {sortedPersons.filter(isNamedPerson).map(p => (
                                        <option key={p.personId} value={p.personId}>{p.name || p.personId}</option>
                                    ))}
                                </optgroup>
                            )}
                            {sortedPersons.some(p => !isNamedPerson(p)) && (
                                <optgroup label="Unnamed">
                                    {sortedPersons.filter(p => !isNamedPerson(p)).map(p => (
                                        <option key={p.personId} value={p.personId}>{p.name || p.personId}</option>
                                    ))}
                                </optgroup>
                            )}
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

            {initialLoading && persons.length === 0 && <Loading label="Loading people…" fullPage={false} />}
            {!initialLoading && persons.length === 0 && view === 'cluster' && (
                <EmptyState
                    icon={<UsersIcon />}
                    title="No people yet"
                    message="As Keepsake finishes processing your photos, faces are grouped into people you can name and search for."
                />
            )}

            {persons.length > 0 && view === 'cluster' && (
                <div className="people-grid">
                    {pagedPersons.map((p, index: number) => {
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
            )}

            {view === 'face' && (
                <>
                    {visibleFaces.length > 0 && (
                        <div className="people-merge-strip face-select-strip">
                            <div className="people-merge-count">{selectedFaceCount} selected</div>
                            <button
                                className="people-secondary-btn"
                                disabled={busy || pagedFaces.length === 0}
                                onClick={selectAllVisibleFaces}
                                type="button"
                            >
                                <CheckIcon />
                                <span>Select page</span>
                            </button>
                            <button
                                className="people-secondary-btn"
                                disabled={busy || selectedFaceCount === 0}
                                onClick={clearSelectedFaces}
                                type="button"
                            >
                                <XMarkIcon />
                                <span>Clear</span>
                            </button>
                            <button
                                className="people-merge-btn people-delete-selected"
                                disabled={busy || selectedFaceCount === 0}
                                aria-label="Delete selected faces"
                                onClick={() => void handleDeleteSelectedFaces()}
                                type="button"
                            >
                                <TrashIcon />
                                <span>Delete selected</span>
                            </button>
                        </div>
                    )}

                    {facesLoading && faces.length === 0 ? (
                        <Loading label="Loading faces…" fullPage={false} />
                    ) : visibleFaces.length === 0 ? (
                        <EmptyState
                            icon={<FaceSmileIcon />}
                            title="No faces to review"
                            message="Individual face crops show up here once your photos finish processing."
                        />
                    ) : (
                        <div className="face-grid">
                            {pagedFaces.map((face, index: number) => {
                                const faceId = face.faceId || '';
                                const suspicious = isSuspiciousFace(face);
                                const isSelected = !!selectedFaces[faceId];
                                return (
                                    <div
                                        key={faceId || `${face.filename}-${face.bbox?.left}-${face.bbox?.top}`}
                                        className={`face-tile ${isSelected ? 'is-selected' : ''} ${suspicious ? 'is-suspicious' : ''}`}
                                        style={{ ['--stagger' as string]: `${Math.min(index, 24) * 16}ms` }}
                                    >
                                        <button
                                            className="face-tile-main"
                                            onClick={() => toggleFace(faceId)}
                                            disabled={!faceId}
                                            type="button"
                                            aria-pressed={isSelected}
                                            title={face.personName ? `Select face • ${face.personName}` : 'Select face'}
                                        >
                                            <PersonAvatarMedia rep={face} alt={face.personName || face.filename || 'Face'} />
                                            {suspicious && (
                                                <span className="person-avatar-review-badge" title="Needs review" aria-label="Needs review">
                                                    <ExclamationTriangleIcon />
                                                </span>
                                            )}
                                        </button>
                                        <span className="face-select-indicator" aria-hidden="true">
                                            <CheckIcon />
                                        </span>
                                        <button
                                            className="face-tile-delete"
                                            onClick={() => faceId && void handleDeleteSingleFace(faceId)}
                                            disabled={!faceId || busy}
                                            type="button"
                                            aria-label="Delete face"
                                            title="Delete face"
                                        >
                                            <TrashIcon />
                                        </button>
                                        {face.personName && <div className="face-caption">{face.personName}</div>}
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </>
            )}

            {totalPages > 1 && (
                <div className="people-pagination">
                    <button
                        className="people-icon-btn"
                        onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                        disabled={page <= 1}
                        aria-label="Previous page"
                        type="button"
                    >
                        <ChevronLeftIcon />
                    </button>
                    <span className="people-pagination-info">Page {page} of {totalPages}</span>
                    <button
                        className="people-icon-btn"
                        onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
                        disabled={page >= totalPages}
                        aria-label="Next page"
                        type="button"
                    >
                        <ChevronRightIcon />
                    </button>
                </div>
            )}

            {suggestions.length > 0 && (
                <div className="people-panel people-suggestions">
                    <div className="people-panel-header">
                        <div className="people-panel-title">Suggestions</div>
                        <div className="people-panel-meta">Faces suggested to merge</div>
                    </div>
                    <div className="people-merge-strip">
                        <div className="people-merge-count">{selectedSuggestionCount} selected</div>
                        <button
                            className="people-secondary-btn"
                            disabled={suggestions.length === 0}
                            onClick={toggleSelectAllSuggestions}
                            type="button"
                        >
                            <CheckIcon />
                            <span>{allSuggestionsSelected ? 'Clear all' : 'Select all'}</span>
                        </button>
                        <button
                            className="people-merge-btn"
                            disabled={selectedSuggestionCount === 0}
                            onClick={() => void handleApproveSelectedSuggestions()}
                            type="button"
                        >
                            <ArrowsRightLeftIcon />
                            <span>Approve {selectedSuggestionCount || ''}</span>
                        </button>
                        <button
                            className="people-secondary-btn people-suggestion-decline"
                            disabled={selectedSuggestionCount === 0}
                            onClick={() => void handleDeclineSelectedSuggestions()}
                            type="button"
                        >
                            <NoSymbolIcon />
                            <span>Decline {selectedSuggestionCount || ''}</span>
                        </button>
                    </div>
                    <div className="people-suggestion-list">
                        {pagedSuggestions.map((s) => {
                            const sourceLabel = getPersonLabel(s.sourcePersonId);
                            const targetLabel = getPersonLabel(s.targetPersonId);
                            const sourceFace = getSuggestionFace(s, 'source');
                            const targetFace = getSuggestionFace(s, 'target');
                            const score = typeof s.similarity === 'number' ? s.similarity : 0;
                            const rowKey = suggestionKey(s);
                            return (
                                <div key={`${s.sourcePersonId}-${s.targetPersonId}`} className={`people-suggestion-row ${selectedSuggestions[rowKey] ? 'is-selected' : ''}`}>
                                    <label className="person-select" title="Select suggestion">
                                        <input
                                            type="checkbox"
                                            checked={!!selectedSuggestions[rowKey]}
                                            onChange={() => toggleSuggestion(rowKey)}
                                        />
                                        <span className="person-select-indicator">
                                            <CheckIcon />
                                        </span>
                                    </label>
                                    <div className="people-suggestion-faces">
                                        <button
                                            className="people-suggestion-face"
                                            onClick={() => s.sourcePersonId && navigate(`/people/${s.sourcePersonId}`)}
                                            type="button"
                                            title={sourceLabel}
                                        >
                                            <span className="people-suggestion-face-avatar" style={getAvatarStyle(s.sourcePersonId)}>
                                                {sourceFace?.filename ? (
                                                    <PersonAvatarMedia rep={sourceFace} alt={sourceLabel} />
                                                ) : (
                                                    <span>{getInitials(s.sourceName, s.sourcePersonId)}</span>
                                                )}
                                            </span>
                                            <span className="people-suggestion-name">{sourceLabel}</span>
                                        </button>
                                        <ArrowsRightLeftIcon className="people-suggestion-arrow" />
                                        <button
                                            className="people-suggestion-face"
                                            onClick={() => s.targetPersonId && navigate(`/people/${s.targetPersonId}`)}
                                            type="button"
                                            title={targetLabel}
                                        >
                                            <span className="people-suggestion-face-avatar" style={getAvatarStyle(s.targetPersonId)}>
                                                {targetFace?.filename ? (
                                                    <PersonAvatarMedia rep={targetFace} alt={targetLabel} />
                                                ) : (
                                                    <span>{getInitials(s.targetName, s.targetPersonId)}</span>
                                                )}
                                            </span>
                                            <span className="people-suggestion-name">{targetLabel}</span>
                                        </button>
                                    </div>
                                    <div className="people-suggestion-meta">
                                        <span className="people-merge-chip">{(score * 100).toFixed(1)}% match</span>
                                    </div>
                                    <div className="people-suggestion-actions">
                                        <button
                                            className="people-merge-btn"
                                            disabled={busy}
                                            onClick={() => void handleMergeSuggestion(s.targetPersonId || '', s.sourcePersonId || '')}
                                            type="button"
                                            aria-label="Merge suggestion"
                                        >
                                            <ArrowsRightLeftIcon />
                                            <span className="sr-only">Merge suggestion</span>
                                        </button>
                                        <button className="people-icon-btn" onClick={() => s.targetPersonId && navigate(`/people/${s.targetPersonId}`)} aria-label="Review suggestion" type="button">
                                            <ArrowRightIcon />
                                        </button>
                                        <button
                                            className="people-icon-btn people-suggestion-decline"
                                            disabled={busy}
                                            onClick={() => void handleDeclineSuggestion(s.sourcePersonId || '', s.targetPersonId || '')}
                                            aria-label="Decline suggestion"
                                            title="Decline suggestion"
                                            type="button"
                                        >
                                            <NoSymbolIcon />
                                        </button>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                    {suggestionTotalPages > 1 && (
                        <div className="people-pagination">
                            <button
                                className="people-icon-btn"
                                onClick={() => setSuggestionPage((prev) => Math.max(1, prev - 1))}
                                disabled={suggestionPage <= 1}
                                aria-label="Previous suggestions page"
                                type="button"
                            >
                                <ChevronLeftIcon />
                            </button>
                            <span className="people-pagination-info">Page {suggestionPage} of {suggestionTotalPages}</span>
                            <button
                                className="people-icon-btn"
                                onClick={() => setSuggestionPage((prev) => Math.min(suggestionTotalPages, prev + 1))}
                                disabled={suggestionPage >= suggestionTotalPages}
                                aria-label="Next suggestions page"
                                type="button"
                            >
                                <ChevronRightIcon />
                            </button>
                        </div>
                    )}
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

export interface AppConfig {
    apiBaseUrl?: string;
    uploadBaseUrl?: string;
    /** Base URL for the dedicated `indexer` container app (workbench action
     * history logging, see routes/tools.py) -- falls back to apiBaseUrl when
     * no separate indexer deployment exists for this environment. */
    indexerApiBaseUrl?: string;
    /** Base URL for the dedicated `recovery` container app (Tools/Workbench
     * recovery actions, see routes/admin.py) -- falls back to apiBaseUrl when
     * no separate recovery deployment exists for this environment. */
    recoveryApiBaseUrl?: string;
    /** Base URL for the dedicated `archive` container app (people/faces,
     * library invites/export/clean, and public share-link routes -- see
     * routes/people.py, routes/library.py, routes/public.py) -- falls back
     * to apiBaseUrl when no separate archive deployment exists for this
     * environment. */
    archiveApiBaseUrl?: string;
    spaBaseUrl?: string;
    azureAdTenantId?: string;
    azureAdClientId?: string;
    azureAdApiScope?: string;
    authMode?: string;
    blazeFaceModelUrl?: string;
    arcFaceModelUrl?: string;
    arcFaceWasmPath?: string;
    buildTimestamp?: string;
    /** Deploy-time processing mode: 'browser' (default, today's behavior) runs
     * OCR/face/vision/geo only client-side; 'backend' skips client-side AI
     * entirely and relies on the vision container; 'both' does both and
     * whichever result lands first wins (see storage_utils._step_locked_done
     * and the processing-lease claim in app.py). */
    processingMode?: 'browser' | 'backend' | 'both';
}

declare global {
    interface Window {
        __APP_CONFIG__?: AppConfig;
    }
}

export const getRuntimeConfig = (): AppConfig => (
    typeof window !== 'undefined' ? (window.__APP_CONFIG__ || {}) : {}
);

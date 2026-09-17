export interface AppConfig {
    apiBaseUrl?: string;
    uploadBaseUrl?: string;
    /** Base URL for the dedicated `tools` container app (workbench action
     * history logging, see routes/tools.py) -- falls back to apiBaseUrl when
     * no separate tools deployment exists for this environment. */
    toolsApiBaseUrl?: string;
    /** Base URL for the dedicated `admin` container app (Tools/Workbench
     * recovery actions, see routes/admin.py) -- falls back to apiBaseUrl when
     * no separate admin deployment exists for this environment. */
    adminApiBaseUrl?: string;
    /** Base URL for the dedicated `extras` container app (people/faces,
     * library invites/export/clean, and public share-link routes -- see
     * routes/people.py, routes/library.py, routes/public.py) -- falls back
     * to apiBaseUrl when no separate extras deployment exists for this
     * environment. */
    extrasApiBaseUrl?: string;
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
     * entirely and relies on the ipworker container; 'both' does both and
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

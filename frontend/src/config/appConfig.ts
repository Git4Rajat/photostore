export interface AppConfig {
    apiBaseUrl?: string;
    uploadBaseUrl?: string;
    /** Base URL for the dedicated `tools` container app (workbench action
     * history logging, see routes/tools.py) -- falls back to apiBaseUrl when
     * no separate tools deployment exists for this environment. */
    toolsApiBaseUrl?: string;
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

export interface AppConfig {
    apiBaseUrl?: string;
    uploadBaseUrl?: string;
    spaBaseUrl?: string;
    azureAdTenantId?: string;
    azureAdClientId?: string;
    azureAdApiScope?: string;
    authMode?: string;
    blazeFaceModelUrl?: string;
    arcFaceModelUrl?: string;
    arcFaceWasmPath?: string;
    buildTimestamp?: string;
}

declare global {
    interface Window {
        __APP_CONFIG__?: AppConfig;
    }
}

export const getRuntimeConfig = (): AppConfig => (
    typeof window !== 'undefined' ? (window.__APP_CONFIG__ || {}) : {}
);

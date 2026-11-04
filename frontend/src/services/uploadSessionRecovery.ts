import { post } from './apiClient';

export interface StaleUploadNotice {
    sessionId: string;
    waveId: string;
    affectedCount: number;
    closedAt: number;
}

const RECOVERY_REQUEST = { singleAttempt: true, timeout: 30000 };
const ACK_REQUEST = { singleAttempt: true, timeout: 15000 };

export const reconcileUploadSessions = () => post<{
    notifications?: StaleUploadNotice[]; pending?: boolean;
}>('/api/upload-sessions/reconcile', {}, RECOVERY_REQUEST);

export const acknowledgeStaleUpload = (sessionId: string) => post(
    `/api/upload-sessions/notifications/${encodeURIComponent(sessionId)}/ack`,
    {}, ACK_REQUEST,
);

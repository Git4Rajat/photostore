import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * A small self-contained upload simulator for the prototypes (frames 11 & 12).
 * Drives a done/total counter, a jittered MB/s readout, and a random tail of
 * failures on completion — enough to exercise the real progress/retry UI
 * without a backend. Intervals are started from event handlers and always
 * cleared on cancel, completion, and unmount.
 */

export interface SimUploadState {
    total: number;
    done: number;
    failedCount: number;
    running: boolean;
    complete: boolean;
    mbps: number;
}

const IDLE: SimUploadState = {
    total: 0,
    done: 0,
    failedCount: 0,
    running: false,
    complete: false,
    mbps: 0,
};

export function useSimulatedUpload() {
    const [state, setState] = useState<SimUploadState>(IDLE);
    const timer = useRef<number | null>(null);

    const stop = useCallback(() => {
        if (timer.current !== null) {
            window.clearInterval(timer.current);
            timer.current = null;
        }
    }, []);

    const cancel = useCallback(() => {
        stop();
        setState((s) => ({ ...s, running: false }));
    }, [stop]);

    const reset = useCallback(() => {
        stop();
        setState(IDLE);
    }, [stop]);

    const start = useCallback(
        (total: number, failRate = 0.02) => {
            stop();
            setState({ total, done: 0, failedCount: 0, running: true, complete: false, mbps: 0 });
            timer.current = window.setInterval(() => {
                setState((s) => {
                    if (!s.running) return s;
                    const step = Math.max(1, Math.round(s.total * (0.04 + Math.random() * 0.06)));
                    const done = Math.min(s.total, s.done + step);
                    if (done >= s.total) {
                        stop();
                        const failedCount = Math.round(s.total * failRate * (0.5 + Math.random()));
                        return { ...s, done: s.total, mbps: 0, running: false, complete: true, failedCount };
                    }
                    return { ...s, done, mbps: 8 + Math.random() * 10 };
                });
            }, 140);
        },
        [stop],
    );

    // Clear any live interval when the frame unmounts.
    useEffect(() => stop, [stop]);

    return { state, start, cancel, reset };
}

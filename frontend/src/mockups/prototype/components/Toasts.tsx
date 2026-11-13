import React from 'react';
import { CircleAlert as ExclamationCircleIcon } from 'lucide-react';
import { useStore } from '../store';
import type { Toast } from '../types';
import { useSwipeDismiss } from '../../../services/swipeDismiss';

/** One toast; a tap on it, or a flick in any direction, closes it early. */
const ToastItem: React.FC<{ toast: Toast; onDismiss: (id: string) => void }> = ({ toast: t, onDismiss }) => {
    const ref = useSwipeDismiss<HTMLDivElement>(() => onDismiss(t.id));
    const isError = t.tone === 'error';
    return (
        <div
            ref={ref}
            className={`pt-toast${isError ? ' error' : ''}`}
            // Errors interrupt (assertive/alert); routine confirmations
            // announce politely without stealing focus.
            role={isError ? 'alert' : 'status'}
            aria-live={isError ? 'assertive' : 'polite'}
        >
            {isError && <ExclamationCircleIcon className="pt-toast-icon" aria-hidden="true" />}
            <span>{t.message}</span>
            {t.actionLabel && (
                <button
                    type="button"
                    className="pt-toast-action"
                    onClick={() => {
                        t.onAction?.();
                        onDismiss(t.id);
                    }}
                >
                    {t.actionLabel}
                </button>
            )}
        </div>
    );
};

/** Bottom-center toast stack with an optional action (e.g. Undo). */
const Toasts: React.FC = () => {
    const { toasts, dismissToast } = useStore();
    if (!toasts.length) return null;
    return (
        <div className="pt-toasts">
            {toasts.map((t) => <ToastItem key={t.id} toast={t} onDismiss={dismissToast} />)}
        </div>
    );
};

export default Toasts;

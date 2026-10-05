import React from 'react';
import { CircleAlert as ExclamationCircleIcon } from 'lucide-react';
import { useStore } from '../store';

/** Bottom-center toast stack with an optional action (e.g. Undo). */
export const Toasts: React.FC = () => {
    const { toasts, dismissToast } = useStore();
    if (!toasts.length) return null;
    return (
        <div className="pt-toasts">
            {toasts.map((t) => {
                const isError = t.tone === 'error';
                return (
                <div
                    key={t.id}
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
                                dismissToast(t.id);
                            }}
                        >
                            {t.actionLabel}
                        </button>
                    )}
                </div>
                );
            })}
        </div>
    );
};

export default Toasts;

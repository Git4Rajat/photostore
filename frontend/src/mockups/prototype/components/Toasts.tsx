import React from 'react';
import { useStore } from '../store';

/** Bottom-center toast stack with an optional action (e.g. Undo). */
export const Toasts: React.FC = () => {
    const { toasts, dismissToast } = useStore();
    if (!toasts.length) return null;
    return (
        <div className="pt-toasts" role="status" aria-live="polite">
            {toasts.map((t) => (
                <div key={t.id} className="pt-toast">
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
            ))}
        </div>
    );
};

export default Toasts;

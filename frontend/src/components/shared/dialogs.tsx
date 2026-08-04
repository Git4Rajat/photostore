import React, { useEffect, useRef, useState } from 'react';

// Promise-based, theme-styled replacements for window.confirm / window.prompt.
// A single <DialogHost /> is mounted in App; confirmDialog()/promptDialog() can
// then be awaited from anywhere (requests queue if one is already open). If the
// host isn't mounted for any reason, we fall back to the native dialogs so no
// caller ever hangs.

export interface ConfirmOptions {
    title?: string;
    message: string;
    confirmLabel?: string;
    cancelLabel?: string;
    // Renders the confirm button in the danger tone for destructive actions.
    danger?: boolean;
}

export interface PromptOptions {
    title?: string;
    message?: string;
    label?: string;
    defaultValue?: string;
    placeholder?: string;
    confirmLabel?: string;
    cancelLabel?: string;
}

type DialogRequest =
    | { kind: 'confirm'; opts: ConfirmOptions; resolve: (value: boolean) => void }
    | { kind: 'prompt'; opts: PromptOptions; resolve: (value: string | null) => void };

let enqueueRequest: ((request: DialogRequest) => void) | null = null;

export const confirmDialog = (opts: ConfirmOptions): Promise<boolean> => {
    if (!enqueueRequest) {
        return Promise.resolve(window.confirm(opts.message));
    }
    return new Promise((resolve) => enqueueRequest!({ kind: 'confirm', opts, resolve }));
};

export const promptDialog = (opts: PromptOptions): Promise<string | null> => {
    if (!enqueueRequest) {
        return Promise.resolve(window.prompt(opts.message || opts.label || opts.title || '', opts.defaultValue ?? ''));
    }
    return new Promise((resolve) => enqueueRequest!({ kind: 'prompt', opts, resolve }));
};

export const DialogHost: React.FC = () => {
    const [queue, setQueue] = useState<DialogRequest[]>([]);
    const queueRef = useRef<DialogRequest[]>(queue);
    queueRef.current = queue;
    const active = queue[0] || null;

    const [inputValue, setInputValue] = useState('');
    const primaryRef = useRef<HTMLButtonElement | null>(null);
    const inputRef = useRef<HTMLInputElement | null>(null);

    useEffect(() => {
        enqueueRequest = (request) => setQueue((prev) => [...prev, request]);
        return () => {
            enqueueRequest = null;
        };
    }, []);

    // Promise resolution is idempotent, so settling is safe even if fired twice.
    const settle = (value: boolean | string | null) => {
        const current = queueRef.current[0];
        if (!current) return;
        if (current.kind === 'confirm') {
            current.resolve(Boolean(value));
        } else {
            current.resolve(value === null ? null : String(value));
        }
        setQueue((prev) => prev.slice(1));
    };

    useEffect(() => {
        if (!active) return undefined;
        setInputValue(active.kind === 'prompt' ? (active.opts.defaultValue ?? '') : '');
        const focusTimer = window.setTimeout(() => {
            if (active.kind === 'prompt') {
                inputRef.current?.select();
            } else {
                primaryRef.current?.focus();
            }
        }, 0);
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                event.stopPropagation();
                settle(active.kind === 'prompt' ? null : false);
            }
        };
        window.addEventListener('keydown', onKeyDown, true);
        return () => {
            window.clearTimeout(focusTimer);
            window.removeEventListener('keydown', onKeyDown, true);
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [active]);

    if (!active) {
        return null;
    }

    const isPrompt = active.kind === 'prompt';
    const opts = active.opts as ConfirmOptions & PromptOptions;
    const cancelValue = isPrompt ? null : false;

    return (
        <div
            className="dialog-backdrop"
            onMouseDown={(event) => {
                if (event.target === event.currentTarget) {
                    settle(cancelValue);
                }
            }}
        >
            <div className="dialog-card" role="dialog" aria-modal="true" aria-label={opts.title || (isPrompt ? 'Input needed' : 'Please confirm')}>
                <p className="dialog-title">{opts.title || (isPrompt ? 'Input needed' : 'Please confirm')}</p>
                {opts.message && <p className="dialog-message">{opts.message}</p>}
                {isPrompt ? (
                    <form
                        className="dialog-form"
                        onSubmit={(event) => {
                            event.preventDefault();
                            settle(inputValue);
                        }}
                    >
                        {opts.label && <label className="dialog-label" htmlFor="dialog-input">{opts.label}</label>}
                        <input
                            id="dialog-input"
                            ref={inputRef}
                            className="field"
                            type="text"
                            value={inputValue}
                            placeholder={opts.placeholder}
                            onChange={(event) => setInputValue(event.target.value)}
                        />
                        <div className="dialog-actions">
                            <button type="button" className="btn btn-soft" onClick={() => settle(null)}>
                                {opts.cancelLabel || 'Cancel'}
                            </button>
                            <button type="submit" className="btn btn-primary">
                                {opts.confirmLabel || 'OK'}
                            </button>
                        </div>
                    </form>
                ) : (
                    <div className="dialog-actions">
                        <button type="button" className="btn btn-soft" onClick={() => settle(false)}>
                            {opts.cancelLabel || 'Cancel'}
                        </button>
                        <button
                            type="button"
                            ref={primaryRef}
                            className={`btn ${opts.danger ? 'btn-danger' : 'btn-primary'}`}
                            onClick={() => settle(true)}
                        >
                            {opts.confirmLabel || 'OK'}
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
};

export default DialogHost;

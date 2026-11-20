import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { CloseGlyph as XMarkIcon } from './RedesignIcons';

/**
 * iOS-style modal sheet: rises from the bottom on phones, a centred dialog on
 * larger screens. Portalled to <body> so it escapes any `.card-glass`/overflow
 * ancestors (whose backdrop-filter/clipping would otherwise trap a fixed child),
 * dims the background, traps Escape, and restores focus to the trigger on close.
 */
export const BottomSheet: React.FC<{
    open: boolean;
    onClose: () => void;
    title?: string;
    children: React.ReactNode;
    /** Optional sticky content directly under the title (e.g. a search field). */
    header?: React.ReactNode;
    labelledBy?: string;
}> = ({ open, onClose, title, children, header }) => {
    const sheetRef = useRef<HTMLDivElement | null>(null);
    const restoreFocusRef = useRef<HTMLElement | null>(null);
    // Callers pass inline arrows; depending on onClose directly would rerun the
    // effect on every parent render (each keystroke), bouncing focus out of the
    // field and dismissing the phone keyboard.
    const onCloseRef = useRef(onClose);
    onCloseRef.current = onClose;

    useEffect(() => {
        if (!open) return undefined;
        restoreFocusRef.current = (document.activeElement as HTMLElement) ?? null;
        const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCloseRef.current(); };
        document.addEventListener('keydown', onKey);
        // Focus the first focusable control (the search field / first row) so the
        // sheet is operable by keyboard and announced by screen readers.
        const first = sheetRef.current?.querySelector<HTMLElement>(
            'input, button:not([disabled]), [href], select, textarea',
        );
        (first ?? sheetRef.current)?.focus();
        return () => {
            document.removeEventListener('keydown', onKey);
            const el = restoreFocusRef.current;
            if (el && typeof el.focus === 'function' && document.contains(el)) el.focus();
        };
    }, [open]);

    if (!open) return null;

    return createPortal(
        <div className="pt-sheet-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
            <div
                ref={sheetRef}
                className="pt-sheet"
                role="dialog"
                aria-modal="true"
                aria-label={title}
                tabIndex={-1}
            >
                <div className="pt-sheet-head">
                    <span className="pt-sheet-grip" aria-hidden="true" />
                    {title && <h2 className="pt-sheet-title">{title}</h2>}
                    <button type="button" className="pt-sheet-close" onClick={onClose} aria-label="Close">
                        <XMarkIcon />
                    </button>
                </div>
                {header && <div className="pt-sheet-header">{header}</div>}
                <div className="pt-sheet-body">{children}</div>
            </div>
        </div>,
        document.body,
    );
};

export default BottomSheet;

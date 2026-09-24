import React from 'react';
import { createPortal } from 'react-dom';

interface SelectionCommandBarProps {
    count: number;
    countLabel?: string;
    children: React.ReactNode;
}

// Floating bottom bar shown whenever a multi-select is active -- shared
// across Gallery/Albums/Recently Deleted so "select something, act on it"
// feels like one consistent gesture regardless of which page you're on,
// instead of three different action-placement patterns.
//
// Portaled to document.body (same pattern as PhotoActionSheet) because
// every host page wraps its content in a .card-glass panel, and
// backdrop-filter/transform on an ancestor makes that ancestor the
// containing block for position:fixed descendants -- without the portal,
// this bar (and any dropdown menu opened from it) gets silently
// clipped/mispositioned inside that panel instead of floating over the
// whole viewport.
const SelectionCommandBar: React.FC<SelectionCommandBarProps> = ({ count, countLabel, children }) => {
    if (count <= 0) {
        return null;
    }
    return createPortal(
        <div className="selection-command-bar" role="toolbar" aria-label="Selection actions">
            <span className="selection-command-bar-count">{countLabel || `${count} selected`}</span>
            <div className="selection-command-bar-actions">{children}</div>
        </div>,
        document.body,
    );
};

export default SelectionCommandBar;

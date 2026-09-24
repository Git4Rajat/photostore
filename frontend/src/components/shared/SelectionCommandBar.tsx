import React from 'react';

interface SelectionCommandBarProps {
    count: number;
    countLabel?: string;
    children: React.ReactNode;
}

// Floating bottom bar shown whenever a multi-select is active -- shared
// across Gallery/Albums/Recently Deleted so "select something, act on it"
// feels like one consistent gesture regardless of which page you're on,
// instead of three different action-placement patterns.
const SelectionCommandBar: React.FC<SelectionCommandBarProps> = ({ count, countLabel, children }) => {
    if (count <= 0) {
        return null;
    }
    return (
        <div className="selection-command-bar" role="toolbar" aria-label="Selection actions">
            <span className="selection-command-bar-count">{countLabel || `${count} selected`}</span>
            <div className="selection-command-bar-actions">{children}</div>
        </div>
    );
};

export default SelectionCommandBar;

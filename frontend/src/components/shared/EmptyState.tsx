import React from 'react';

interface EmptyStateProps {
    // Typically a heroicon; rendered at a fixed size in the accent color.
    icon?: React.ReactNode;
    title: string;
    message?: string;
    // Optional call-to-action (e.g. a button or link).
    action?: React.ReactNode;
    className?: string;
}

// A warm, branded empty state for zero-data views (empty gallery, no people
// yet, empty album). Replaces terse one-line "nothing here" text.
export const EmptyState: React.FC<EmptyStateProps> = ({ icon, title, message, action, className }) => (
    <div className={`empty-state${className ? ` ${className}` : ''}`}>
        {icon && (
            <span className="empty-state-icon" aria-hidden="true">
                {icon}
            </span>
        )}
        <p className="empty-state-title">{title}</p>
        {message && <p className="empty-state-message">{message}</p>}
        {action && <div className="empty-state-action">{action}</div>}
    </div>
);

export default EmptyState;

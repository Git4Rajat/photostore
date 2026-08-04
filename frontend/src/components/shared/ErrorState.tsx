import React from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';

interface ErrorStateProps {
    title?: string;
    message: string;
    onRetry?: () => void;
    retryLabel?: string;
    className?: string;
}

// Branded error panel for page-level load failures. Shares the empty-state
// layout with a danger-toned icon and an optional retry action.
export const ErrorState: React.FC<ErrorStateProps> = ({
    title = 'Something went wrong',
    message,
    onRetry,
    retryLabel = 'Try again',
    className,
}) => (
    <div className={`empty-state error-state${className ? ` ${className}` : ''}`} role="alert">
        <span className="empty-state-icon error-state-icon" aria-hidden="true">
            <ExclamationTriangleIcon />
        </span>
        <p className="empty-state-title">{title}</p>
        {message && <p className="empty-state-message">{message}</p>}
        {onRetry && (
            <div className="empty-state-action">
                <button type="button" className="btn btn-primary" onClick={onRetry}>
                    {retryLabel}
                </button>
            </div>
        )}
    </div>
);

export default ErrorState;

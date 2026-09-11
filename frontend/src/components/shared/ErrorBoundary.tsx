import React from 'react';
import { ErrorState } from './ErrorState';

interface ErrorBoundaryProps {
    children: React.ReactNode;
    // Static fallback, or a render prop that gets the boundary's own reset
    // function (e.g. to wire a custom "back" / "try again" action). Omit to
    // get the default full ErrorState panel -- pass `null` for a spot (a
    // header icon, one grid tile) where disappearing quietly beats showing a
    // panel sized for a whole page/section.
    fallback?: React.ReactNode | ((reset: () => void) => React.ReactNode);
    title?: string;
    message?: string;
    // Logged alongside the error so console output says which boundary
    // caught it, e.g. "[ErrorBoundary:gallery-tile]".
    context?: string;
}

interface ErrorBoundaryState {
    error: Error | null;
}

// Contains a render-time crash to the subtree it wraps instead of letting it
// bubble up and unmount the whole app (React's default behavior since v18).
// Only catches errors thrown while React is rendering this subtree -- errors
// in event handlers, timers, or async/await code (the browser-AI processing
// pipeline, upload loop, etc.) never reach componentDidCatch and must still
// be try/caught at the source.
export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
    state: ErrorBoundaryState = { error: null };

    static getDerivedStateFromError(error: Error): ErrorBoundaryState {
        return { error };
    }

    componentDidCatch(error: Error, info: React.ErrorInfo) {
        console.error(`[ErrorBoundary${this.props.context ? `:${this.props.context}` : ''}]`, error, info.componentStack);
    }

    reset = () => this.setState({ error: null });

    render() {
        const { error } = this.state;
        if (!error) {
            return this.props.children;
        }

        const { fallback } = this.props;
        if (typeof fallback === 'function') {
            return fallback(this.reset);
        }
        if (fallback !== undefined) {
            return fallback;
        }

        return (
            <ErrorState
                title={this.props.title || 'Something went wrong'}
                message={this.props.message || 'This part of the page hit an unexpected error. You can try again, or use the navigation above to go elsewhere.'}
                onRetry={this.reset}
            />
        );
    }
}

export default ErrorBoundary;

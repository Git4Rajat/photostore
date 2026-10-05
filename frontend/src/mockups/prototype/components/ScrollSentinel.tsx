import React from 'react';

/** Fires ``onVisible`` whenever it scrolls into view (and again after each page lands), for infinite scroll. */
export const ScrollSentinel: React.FC<{ onVisible: () => void; deps: number }> = ({ onVisible, deps }) => {
    const ref = React.useRef<HTMLDivElement>(null);
    React.useEffect(() => {
        const node = ref.current;
        if (!node) return undefined;
        const observer = new IntersectionObserver((entries) => {
            if (entries.some((e) => e.isIntersecting)) onVisible();
        }, { rootMargin: '600px' });
        observer.observe(node);
        return () => observer.disconnect();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [deps]);
    return <div className="pt-scroll-sentinel" ref={ref} aria-hidden="true" />;
};

export default ScrollSentinel;

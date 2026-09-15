import React from 'react';

interface MetricCardProps {
    value: React.ReactNode;
    label: string;
}

const MetricCard: React.FC<MetricCardProps> = ({ value, label }) => (
    <article className="albums-metric-card">
        <p className="albums-metric-value">{value}</p>
        <p className="albums-metric-label">{label}</p>
    </article>
);

export default MetricCard;

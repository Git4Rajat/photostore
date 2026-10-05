import React from 'react';
import { useIndexBuilding } from '../../../services/indexBuilding';

/** Slim indeterminate bar shown on every page while the server is still preparing the library. */
export const IndexBuildBar: React.FC = () => {
    const building = useIndexBuilding();
    if (!building) return null;
    return (
        <div className="pt-index-bar" role="status" aria-live="polite">
            <div className="pt-index-bar-track" aria-hidden="true"><div className="pt-index-bar-fill" /></div>
            <span className="pt-index-bar-text">Preparing your library — photos, search and people appear as they’re ready.</span>
        </div>
    );
};

export default IndexBuildBar;

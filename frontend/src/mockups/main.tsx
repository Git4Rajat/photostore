import React from 'react';
import { createRoot } from 'react-dom/client';
import '../index.css';
import './mockups.css';
import './prototype/prototype.css';
import PrototypeApp from './prototype/PrototypeApp';

// Standalone entry for the fully-functional Keepsake prototype. Kept entirely
// separate from the app's src/index.tsx so nothing here affects the shipped
// bundle. View at http://localhost:3000/mockups.html.
const rootElement = document.getElementById('root');
if (rootElement) {
    const root = createRoot(rootElement);
    root.render(
        <React.StrictMode>
            <PrototypeApp />
        </React.StrictMode>
    );
}

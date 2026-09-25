import React from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import './mockups/mockups.css';
import './mockups/prototype/prototype.css';
import PrototypeApp from './mockups/prototype/PrototypeApp';

const rootElement = document.getElementById('root');
if (rootElement) {
  const root = createRoot(rootElement);
  root.render(
    <React.StrictMode>
      <PrototypeApp />
    </React.StrictMode>
  );
}
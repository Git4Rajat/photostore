import React from 'react';
import { useStore } from '../store';
import { FloatingSelectButton } from './FloatingSelectButton';

/** Binds the floating Select/Done pill to the store's global select mode. */
export const StoreFloatingSelectButton: React.FC = () => {
    const { selectMode, setSelectMode } = useStore();
    return <FloatingSelectButton selectMode={selectMode} onToggle={() => setSelectMode(!selectMode)} />;
};

import React from 'react';
import { useStore } from '../store';
import { FloatingSelectButton } from './FloatingSelectButton';

/** Binds the floating Select/Done pill to the store's global select mode; yields to the bottom
 *  selection bar (which has its own close button) once photos are selected. Photo tiles start
 *  selecting on long-press, so touch screens don't get the pill at all. */
export const StoreFloatingSelectButton: React.FC = () => {
    const { selectMode, setSelectMode, selection } = useStore();
    return <FloatingSelectButton selectMode={selectMode} onToggle={() => setSelectMode(!selectMode)} suppressed={selection.length > 0} hideOnTouch />;
};

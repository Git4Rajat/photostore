export const shouldSuppressLeaseWarning = (err: unknown): boolean => {
    const message = typeof err === 'string' ? err : String(err || '');
    return /photo not found/i.test(message)
        || /lease_active/i.test(message)
        || /already held by another client/i.test(message)
        || /processing lease/i.test(message)
        || /\b409\b/.test(message);
};

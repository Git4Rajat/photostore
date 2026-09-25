import React, { useState } from 'react';
import { PaperAirplaneIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { Avatar } from '../components/bits';
import type { Member } from '../types';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Sharing — family library members, invites, roles, and library settings. */
export const SharingPage: React.FC = () => {
    const { members, invite, cancelInvite, resendInvite, removeMember, setMemberRole, toast } = useStore();
    const [email, setEmail] = useState('');
    const [role, setRole] = useState<Member['role']>('contribute');
    const [exportRunning, setExportRunning] = useState(true);
    const valid = EMAIL_RE.test(email.trim());

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Sharing</h1>
                    <p className="pt-page-sub">Home Library · {members.length} {members.length === 1 ? 'person' : 'people'}</p>
                </div>
            </div>

            <div className="card-glass lib-card">
                {members.map((m) => (
                    <div key={m.id} className={`member-row${m.pending ? ' pending' : ''}`}>
                        <Avatar initials={m.initials} color={m.color || undefined} />
                        <span className="member-meta"><b>{m.name}</b><span>{m.sub}</span></span>
                        {m.role === 'owner' && <span className="member-badge owner">Owner</span>}
                        {m.pending ? (
                            <>
                                <button type="button" className="mock-link" onClick={() => resendInvite(m.id)}>Resend</button>
                                <button type="button" className="mock-link muted" onClick={() => cancelInvite(m.id)}>Cancel</button>
                            </>
                        ) : m.role !== 'owner' ? (
                            <>
                                <select className="field member-role" value={m.role} onChange={(e) => setMemberRole(m.id, e.target.value as Member['role'])} aria-label={`Role for ${m.name}`}>
                                    <option value="view">Can view</option>
                                    <option value="contribute">Can view &amp; add</option>
                                </select>
                                <button type="button" className="mock-link muted" onClick={() => removeMember(m.id)}>Remove</button>
                            </>
                        ) : null}
                    </div>
                ))}

                <div className="invite-bar">
                    <input className="field" type="email" placeholder="Invite by email" value={email} onChange={(e) => setEmail(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && valid) { invite(email.trim(), role); setEmail(''); } }} />
                    <select className="field field-select" value={role} onChange={(e) => setRole(e.target.value as Member['role'])} aria-label="Permission level">
                        <option value="view">Can view</option>
                        <option value="contribute">Can view &amp; add</option>
                    </select>
                    <button type="button" className="btn mock-cta" disabled={!valid} onClick={() => { invite(email.trim(), role); setEmail(''); }}>
                        <PaperAirplaneIcon className="toolbar-icon" /> Send invite
                    </button>
                </div>
            </div>

            <div className="card-glass lib-card">
                <div className="pt-menu-label">Library settings</div>
                <div className={`upload-dock${exportRunning ? '' : ' is-done'}`}>
                    {exportRunning ? (
                        <>
                            <span className="count">Exporting 3.2 / 9.4 GB</span>
                            <span className="track"><span className="fill" style={{ width: '34%' }} /></span>
                            <span className="rate">18.6 MB/s</span>
                            <button type="button" className="btn" onClick={() => setExportRunning(false)}>Cancel</button>
                        </>
                    ) : (
                        <>
                            <span className="done-label">Export canceled</span>
                            <span className="track"><span className="fill" style={{ width: '34%' }} /></span>
                            <button type="button" className="btn" onClick={() => setExportRunning(true)}>Restart</button>
                        </>
                    )}
                </div>
                <div className="pt-danger-row">
                    <button type="button" className="btn" onClick={() => toast('Rename library')}>Rename library</button>
                    <button type="button" className="btn" onClick={() => toast('Clean library scheduled')}>Clean library</button>
                    <button type="button" className="btn btn-danger" onClick={() => toast('Delete library — needs confirmation')}>Delete library</button>
                </div>
            </div>
        </div>
    );
};

export default SharingPage;

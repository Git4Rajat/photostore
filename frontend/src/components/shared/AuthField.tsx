import React, { useState } from 'react';
import { Eye as EyeIcon, EyeOff as EyeOffIcon } from 'lucide-react';

interface AuthFieldProps {
    id: string;
    label: string;
    type?: 'text' | 'email' | 'password';
    value: string;
    onChange: (value: string) => void;
    autoComplete?: string;
    autoFocus?: boolean;
    invalid?: boolean;
    // Sits on the label row, right-aligned ("Forgot password?").
    labelAction?: React.ReactNode;
    describedBy?: string;
    // Shown but not editable (an invited email address).
    readOnly?: boolean;
}

// A labelled field from the sign-in mock-up: the label stays above the input, an invalid field is
// ringed, and a password field gets the eye that shows what was typed.
const AuthField: React.FC<AuthFieldProps> = ({
    id, label, type = 'text', value, onChange, autoComplete, autoFocus, invalid = false, labelAction, describedBy, readOnly = false,
}) => {
    const [shown, setShown] = useState(false);
    const isPassword = type === 'password';
    return (
        <div className="auth-fg">
            <div className="auth-lblr">
                <label htmlFor={id}>{label}</label>
                {labelAction}
            </div>
            <div className={`auth-field${invalid ? ' err' : ''}${readOnly ? ' ro' : ''}`}>
                <input
                    id={id}
                    type={isPassword && shown ? 'text' : type}
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    autoComplete={autoComplete}
                    autoFocus={autoFocus}
                    readOnly={readOnly}
                    aria-invalid={invalid || undefined}
                    aria-describedby={describedBy}
                    autoCapitalize="none"
                    spellCheck={false}
                />
                {isPassword && (
                    <button
                        type="button"
                        className="auth-eye"
                        onClick={() => setShown((v) => !v)}
                        aria-label={shown ? 'Hide password' : 'Show password'}
                        aria-pressed={shown}
                    >
                        {shown ? <EyeOffIcon aria-hidden="true" /> : <EyeIcon aria-hidden="true" />}
                    </button>
                )}
            </div>
        </div>
    );
};

export default AuthField;

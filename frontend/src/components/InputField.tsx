// Labeled input with icon; password inputs get a show/hide toggle
import { useState, type InputHTMLAttributes } from 'react';
import { Eye, EyeOff, type LucideIcon } from 'lucide-react';

interface InputFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  icon?: LucideIcon;
}

export default function InputField({ label, icon: Icon, type = 'text', ...rest }: InputFieldProps) {
  const [showPassword, setShowPassword] = useState(false);
  const isPassword = type === 'password';

  return (
    <label className="field">
      <span>{label}</span>
      <div className="field__control">
        {Icon && <Icon size={18} />}
        <input type={isPassword && showPassword ? 'text' : type} {...rest} />
        {isPassword && (
          <button
            type="button"
            className="icon-button"
            onClick={() => setShowPassword(!showPassword)}
            aria-label={showPassword ? 'Hide password' : 'Show password'}
          >
            {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
          </button>
        )}
      </div>
    </label>
  );
}

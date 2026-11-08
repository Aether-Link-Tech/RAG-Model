import { TriangleAlert } from 'lucide-react';

export default function Alert({ message }: { message: string }) {
  if (!message) return null;
  return (
    <div className="form-alert" role="alert">
      <TriangleAlert size={18} />
      <span>{message}</span>
    </div>
  );
}

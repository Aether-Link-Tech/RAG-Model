import logo from '../assets/images/aether-link-logo.jpg';

interface LogoProps {
  showText?: boolean;
}

export default function Logo({ showText = true }: LogoProps) {
  return (
    <div className="brand">
      <span className="brand__mark">
        <img src={logo} alt="Aether Link logo" />
      </span>
      {showText && (
        <span>
          <strong>Aether Link</strong>
          <small>Enterprise RAG</small>
        </span>
      )}
    </div>
  );
}

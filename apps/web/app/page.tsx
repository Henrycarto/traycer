import Link from 'next/link';
import { AlertFeed } from '@/components/pulse/AlertFeed';
import { TrajectoryChart } from '@/components/pulse/TrajectoryChart';
import { demoAlerts, demoTrajectorySeries } from '@/lib/demo-data';

export const metadata = {
  title: 'Traycer',
  description:
    'Psychiatric relapse follows a detectable behavioral pattern up to three weeks before clinical deterioration.',
};

export default function LandingPage() {
  const series = demoTrajectorySeries('TRC-4f21a9c4', 'crossing', 41);
  const alerts = demoAlerts();

  return (
    <div className="mx-auto w-full max-w-[1400px] px-6">
      {/* The statement comes first. Nothing above it, no eyebrow label, no
          badge, no logo strip. A psychiatrist reading two sentences either
          recognises the problem or does not, and everything below this block
          is only worth reading if they do. */}
      <section className="border-b border-line py-16">
        <h1 className="max-w-[52ch] text-[26px] font-normal leading-[1.45] text-text-primary sm:text-[30px]">
          Psychiatric relapse follows a detectable behavioral pattern up to three weeks before
          clinical deterioration. Traycer reads it from the phone already in your patient&apos;s
          pocket.
        </h1>
      </section>

      <section className="grid gap-6 py-8 lg:grid-cols-[minmax(0,1.65fr)_minmax(0,1fr)]">
        <TrajectoryChart series={series} height={300} />
        <AlertFeed alerts={alerts} linkPatients={false} maxRows={6} />
      </section>

      <section className="border-t border-line py-10">
        <p className="max-w-[68ch] text-[15px] leading-relaxed text-text-secondary">
          The chart above is one patient over thirty days. The dashed line is their own
          behavioral baseline, built from their first fourteen days of data. The shaded band
          starts where deviation from that baseline becomes clinically meaningful. On day
          twenty two their movement radius began contracting, their phone use moved into the
          night, and their contact with other people dropped. None of that was reported. The
          phone measured it.
        </p>
      </section>

      <section className="grid gap-px border border-line bg-line md:grid-cols-2">
        <Block
          title="No input from the patient"
          body="No journaling, no mood scales, no check-ins. Adherence to self report collapses exactly when a patient is deteriorating, which is when the data matters most. Traycer collects nothing that requires the patient to do anything."
        />
        <Block
          title="Scored against themselves"
          body="Every patient is compared to their own fourteen day baseline, never to a population. A shift worker, a retiree and a graduate student have nothing in common on a population distribution, and all three produce a clear signal when their own pattern breaks."
        />
        <Block
          title="Fifteen numbers, nothing else"
          body="Motion, mobility, phone usage and social interaction proxies, aggregated on device into fifteen values per window. No coordinates, no message content, no contact list, no app names. The privacy boundary is enforced in code, not in a policy document."
        />
        <Block
          title="Weeks, not days"
          body="Behavioral change precedes clinical presentation. Detection at three weeks turns a hospital admission into a medication review and an extra appointment. That interval is the entire clinical argument for the product."
        />
      </section>

      <section className="border-x border-b border-line px-6 py-8">
        <h2 className="text-[15px] font-medium text-text-primary">Three components</h2>
        <dl className="mt-5 flex flex-col gap-5">
          <Component
            name="Traycer SDK"
            state="open source"
            body="A React Native background sensing library, published as @traycer/sdk. Third party mental health apps embed it and become the distribution channel. Battery conscious by design: one short accelerometer burst per hour rather than a continuous stream."
          />
          <Component
            name="Traycer Voice"
            state="demo ready"
            body="An openSMILE microservice that turns an optional short voice sample into an acoustic vector. Vocal energy, speech rate, pitch variance, pause frequency. It never transcribes, and a content guard in the pipeline enforces that rather than promising it."
          />
          <Component
            name="Traycer Pulse"
            state="dashboard"
            body="The clinician view. Trajectory scores across a patient panel, alerts ranked by severity, and push notification when a patient's behavioral signature moves away from their own baseline and stays there."
          />
        </dl>
      </section>

      <section className="border-x border-b border-line px-6 py-8">
        <h2 className="text-[15px] font-medium text-text-primary">Embed the SDK</h2>
        <p className="mt-3 max-w-[68ch] text-[14px] leading-relaxed text-text-secondary">
          The sensing layer is open source and published. The scoring model is not. If you run
          a mental health app, you can collect the behavioral signal today and decide about the
          scoring service separately.
        </p>
        <pre className="mt-4 overflow-x-auto border border-line bg-void px-4 py-3 font-mono text-[12px] text-text-secondary">
          <code>npm install @traycer/sdk</code>
        </pre>
        <div className="mt-5 flex flex-wrap gap-3">
          <Link href="/dashboard" className="btn-primary">
            Open the dashboard
          </Link>
          <a
            href="https://github.com/Henrycarto/traycer/tree/main/packages/sdk"
            className="btn-quiet"
          >
            Read the SDK integration guide
          </a>
        </div>
      </section>

      <section className="border-x border-b border-line px-6 py-8">
        <h2 className="text-[15px] font-medium text-text-primary">Build status</h2>
        <ul className="mt-4 flex flex-col gap-2.5">
          <Status
            state="ready"
            label="Traycer SDK"
            detail="Complete and publishable. Sensors, batched encrypted upload, and the data minimizer are implemented and tested."
          />
          <Status
            state="ready"
            label="Scoring architecture"
            detail="Personal baseline construction and deviation scoring are implemented. The coefficients that define a clinically significant deviation are private."
          />
          <Status
            state="demo"
            label="Traycer Voice"
            detail="openSMILE integration and the content guard are working. Acoustic thresholds are not clinically validated."
          />
          <Status
            state="blocked"
            label="Trajectory prediction"
            detail="The LSTM interface is finished and integrated. The model is untrained and requires a labelled clinical corpus with confirmed relapse dates."
          />
        </ul>
      </section>

      <section className="border-x border-b border-line px-6 py-8">
        <p className="max-w-[68ch] text-[13px] leading-relaxed text-text-muted">
          Passive sensing without informed consent is surveillance. Traycer is built to be
          deployed inside a treatment relationship, with the patient told what is measured and
          able to stop it. The technical controls in the SDK do not substitute for that
          conversation.
        </p>
      </section>
    </div>
  );
}

function Block({ title, body }: { title: string; body: string }) {
  return (
    <div className="bg-surface px-6 py-6">
      <h3 className="text-[14px] font-medium text-text-primary">{title}</h3>
      <p className="mt-2 max-w-[52ch] text-[13px] leading-relaxed text-text-secondary">{body}</p>
    </div>
  );
}

function Component({ name, state, body }: { name: string; state: string; body: string }) {
  return (
    <div className="grid gap-2 sm:grid-cols-[13rem_1fr] sm:gap-6">
      <div className="flex flex-col gap-1">
        <dt className="font-mono text-[13px] text-text-primary">{name}</dt>
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-indigo">
          {state}
        </span>
      </div>
      <dd className="max-w-[62ch] text-[13px] leading-relaxed text-text-secondary">{body}</dd>
    </div>
  );
}

const STATUS_COLOR: Record<string, string> = {
  ready: 'bg-stable',
  demo: 'bg-alert-medium',
  blocked: 'bg-inactive',
};

function Status({ state, label, detail }: { state: string; label: string; detail: string }) {
  return (
    <li className="grid grid-cols-[10px_11rem_1fr] items-start gap-3">
      <span className={`mt-1.5 h-2 w-2 ${STATUS_COLOR[state]}`} aria-hidden />
      <span className="font-mono text-[12px] text-text-primary">{label}</span>
      <span className="max-w-[62ch] text-[13px] leading-relaxed text-text-secondary">
        {detail}
      </span>
    </li>
  );
}

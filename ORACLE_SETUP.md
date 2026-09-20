# Run the finder + CRM 24/7 on Oracle Cloud Always Free (PC can stay off)

What you get: the watcher and the CRM running on a free Ubuntu VM that never
sleeps. You open the CRM from your PC/phone browser exactly like now. Your PC
can be off.

Cost: $0 if you stay on Always Free shapes (see step 1 + warnings at the end).

## 1. Create the free VM

1. Sign up at cloud.oracle.com (asks for a card to verify, not charged).
2. Create a Compute instance:
   - Image: **Ubuntu 24.04** (or 22.04).
   - Shape: **VM.Standard.A1.Flex** (Ampere ARM — up to 4 CPUs / 24 GB free)
     or **VM.Standard.E2.1.Micro** (AMD — 1 CPU / 1 GB free). Either runs this
     fine; Ampere gives more headroom. If your region says "out of capacity"
     for Ampere, try E2 Micro or another home region.
   - Add your SSH public key (generate one with `ssh-keygen` on your PC if you
     don't have one). Save the private key file.
   - Leave the firewall as-is (only port 22). **Do NOT open port 8780** — the
     CRM has no login and must never face the internet (step 6 covers access).
3. Note the VM's **public IP**.

## 2. First login + install Python

```bash
ssh ubuntu@<VM_PUBLIC_IP>
sudo apt update && sudo apt install -y python3 python3-pip
python3 -c "import requests" 2>/dev/null || pip install --break-system-packages requests
```

## 3. Upload the project

From PowerShell on your PC (in this folder):

```powershell
scp -r . ubuntu@<VM_PUBLIC_IP>:~/robloxfind
```

That copies everything including history. (First run only; later updates: just
`scp` the files you changed.)

## 4. Store the cookie (server-side, never in a file in the project)

```bash
sudo nano /etc/robloxfind.env
```

Contents (one line, your throwaway-alt cookie):

```
ROBLOSECURITY=_|WARNING:-DO-NOT-SHARE-THIS....your cookie value
```

```bash
sudo chmod 600 /etc/robloxfind.env
```

## 5. Install + start the services

```bash
cd ~/robloxfind
sudo cp roblox-finder.service roblox-crm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now roblox-finder roblox-crm
```

Check they run:

```bash
systemctl status roblox-finder roblox-crm --no-pager
tail -20 ~/robloxfind/finder.log        # watcher progress
journalctl -u roblox-crm -n 20 --no-pager   # CRM boot line
```

## 6. Reach the CRM securely (Tailscale, free)

On the VM:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4     # note this 100.x.y.z address
```

On your PC (and phone if you want): install Tailscale from tailscale.com,
log in with the **same account**, then open:

```
http://<tailscale-ip-of-vm>:8780
```

Use it exactly like before (Sync, Track, statuses). Nothing else on the
internet can reach it.

## 7. Stop the PC watcher

Close the `start_finder.bat` window (and remove the Startup entry if you added
one). Two watchers = double work and double rate-limit pressure. Keep
`start_crm.bat` only as a local backup for when you want to work offline.

## 8. Day-to-day

```bash
tail -30 ~/robloxfind/finder.log                    # what is it doing now?
systemctl restart roblox-finder roblox-crm           # after uploading new code
journalctl -u roblox-finder -n 50 --no-pager        # crash debugging
```

- **Update code:** `scp` the changed `.py`/`.html` files, then
  `sudo systemctl restart roblox-finder roblox-crm`.
- **Back up:** copy `~/robloxfind/crm_data.json` to your PC occasionally —
  that file is your statuses, notes and radar. Everything else regenerates.
- **Rotate the cookie** (alt account): update `/etc/robloxfind.env`, then
  `sudo systemctl restart roblox-finder roblox-crm`.

## Warnings (read once)

- Oracle can reclaim **idle** Always Free VMs. A running watcher counts as
  activity, but log in every few weeks and glance at the console anyway.
- Set a **billing alarm** ($0 threshold) in the Oracle console so a misclicked
  paid shape can never surprise you. Always pick shapes marked "Always Free".
- The CRM still has no password — that is fine *only* because port 8780 is
  closed publicly and you access it over Tailscale. Never open that port.
- One watcher at a time. If you ever run the PC copy again for testing, stop
  the cloud one first (`sudo systemctl stop roblox-finder`).

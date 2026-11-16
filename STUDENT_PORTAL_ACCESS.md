# Student portal access

The teacher dashboard and student-account page link to the existing `/student/login` route. The Copy Student Login Link button copies the configured portal URL; it does not create a second authentication flow.

Set `PUBLIC_BASE_URL` to the origin students can reach:

- **Local-only:** leave it unset while testing on the teacher's computer. The page uses the local loopback URL. Other devices cannot use `localhost` to reach the teacher's computer.
- **LAN:** set `PUBLIC_BASE_URL` to the teacher computer's reachable LAN address and port, such as `http://192.168.1.25:8000`. Start the application bound to a LAN interface (for example `0.0.0.0`) and permit the port in the host firewall. Plain HTTP does not encrypt passwords; use HTTPS for networks that are not fully trusted.
- **Deployed:** set `PUBLIC_BASE_URL` to the public HTTPS origin behind the deployment's reverse proxy, such as `https://quiz.example.edu`. Configure HTTPS and the deployment's network access controls before distributing credentials.

When `PUBLIC_BASE_URL` is unset, the application only constructs a link from loopback or the local test client. Requests arriving on another host require explicit configuration; the application does not guess a public deployment address from an arbitrary Host header.

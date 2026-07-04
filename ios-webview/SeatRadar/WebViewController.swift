import UIKit
import WebKit

final class WebViewController: UIViewController {
    private enum Constants {
        static let homeURL = URL(string: "http://103.203.140.44:18000/")!
        static let internalHost = "103.203.140.44"
        static let progressHeight: CGFloat = 2
    }

    private lazy var webView: WKWebView = {
        let preferences = WKWebpagePreferences()
        preferences.allowsContentJavaScript = true

        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences = preferences
        configuration.allowsInlineMediaPlayback = true

        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = self
        view.uiDelegate = self
        view.allowsBackForwardNavigationGestures = true
        view.customUserAgent = "SeatRadarIOS/1.0"
        view.translatesAutoresizingMaskIntoConstraints = false
        return view
    }()

    private let progressView: UIProgressView = {
        let view = UIProgressView(progressViewStyle: .bar)
        view.progressTintColor = UIColor(red: 18 / 255, green: 141 / 255, blue: 97 / 255, alpha: 1)
        view.trackTintColor = .clear
        view.translatesAutoresizingMaskIntoConstraints = false
        return view
    }()

    private let errorView: UIView = {
        let container = UIView()
        container.backgroundColor = UIColor(red: 246 / 255, green: 248 / 255, blue: 244 / 255, alpha: 1)
        container.isHidden = true
        container.translatesAutoresizingMaskIntoConstraints = false
        return container
    }()

    private let errorLabel: UILabel = {
        let label = UILabel()
        label.text = "页面加载失败"
        label.textColor = UIColor(red: 31 / 255, green: 91 / 255, blue: 78 / 255, alpha: 1)
        label.font = .systemFont(ofSize: 17, weight: .semibold)
        label.textAlignment = .center
        label.translatesAutoresizingMaskIntoConstraints = false
        return label
    }()

    private let retryButton: UIButton = {
        let button = UIButton(type: .system)
        button.setTitle("重新加载", for: .normal)
        button.titleLabel?.font = .systemFont(ofSize: 15, weight: .semibold)
        button.tintColor = .white
        button.backgroundColor = UIColor(red: 18 / 255, green: 141 / 255, blue: 97 / 255, alpha: 1)
        button.layer.cornerRadius = 8
        button.contentEdgeInsets = UIEdgeInsets(top: 10, left: 18, bottom: 10, right: 18)
        button.translatesAutoresizingMaskIntoConstraints = false
        return button
    }()

    private var progressObservation: NSKeyValueObservation?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 246 / 255, green: 248 / 255, blue: 244 / 255, alpha: 1)
        buildLayout()
        bindProgress()
        retryButton.addTarget(self, action: #selector(reloadHome), for: .touchUpInside)
        loadHome()
    }

    deinit {
        progressObservation?.invalidate()
    }

    private func buildLayout() {
        view.addSubview(webView)
        view.addSubview(progressView)
        view.addSubview(errorView)
        errorView.addSubview(errorLabel)
        errorView.addSubview(retryButton)

        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),

            progressView.topAnchor.constraint(equalTo: webView.topAnchor),
            progressView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            progressView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            progressView.heightAnchor.constraint(equalToConstant: Constants.progressHeight),

            errorView.topAnchor.constraint(equalTo: view.topAnchor),
            errorView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            errorView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            errorView.bottomAnchor.constraint(equalTo: view.bottomAnchor),

            errorLabel.centerXAnchor.constraint(equalTo: errorView.centerXAnchor),
            errorLabel.centerYAnchor.constraint(equalTo: errorView.centerYAnchor, constant: -28),

            retryButton.topAnchor.constraint(equalTo: errorLabel.bottomAnchor, constant: 16),
            retryButton.centerXAnchor.constraint(equalTo: errorView.centerXAnchor)
        ])
    }

    private func bindProgress() {
        progressObservation = webView.observe(\.estimatedProgress, options: [.new]) { [weak self] webView, _ in
            guard let self else { return }
            progressView.progress = Float(webView.estimatedProgress)
            progressView.isHidden = webView.estimatedProgress >= 1
        }
    }

    private func loadHome() {
        errorView.isHidden = true
        webView.load(URLRequest(url: Constants.homeURL))
    }

    @objc private func reloadHome() {
        loadHome()
    }

    private func normalizedInternalURL(from url: URL) -> URL {
        guard url.scheme == nil else {
            return url
        }
        if url.absoluteString.hasPrefix("/") {
            return URL(string: url.absoluteString, relativeTo: Constants.homeURL)?.absoluteURL ?? Constants.homeURL
        }
        return URL(string: url.absoluteString, relativeTo: Constants.homeURL)?.absoluteURL ?? Constants.homeURL
    }

    private func shouldKeepInsideApp(_ url: URL) -> Bool {
        let resolvedURL = normalizedInternalURL(from: url)
        guard let scheme = resolvedURL.scheme?.lowercased() else {
            return true
        }
        if scheme == "http" || scheme == "https" {
            return resolvedURL.host == Constants.internalHost
        }
        return false
    }

    private func openExternally(_ url: URL) {
        let resolvedURL = normalizedInternalURL(from: url)
        UIApplication.shared.open(resolvedURL, options: [:])
    }
}

extension WebViewController: WKNavigationDelegate {
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }

        if shouldKeepInsideApp(url) {
            decisionHandler(.allow)
            return
        }

        openExternally(url)
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        errorView.isHidden = true
        progressView.isHidden = false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        errorView.isHidden = true
        progressView.isHidden = true
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        errorView.isHidden = false
        progressView.isHidden = true
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        errorView.isHidden = false
        progressView.isHidden = true
    }
}

extension WebViewController: WKUIDelegate {
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        guard let url = navigationAction.request.url else {
            return nil
        }
        if shouldKeepInsideApp(url) {
            webView.load(URLRequest(url: normalizedInternalURL(from: url)))
        } else {
            openExternally(url)
        }
        return nil
    }
}

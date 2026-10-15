cask "gluco" do
  version "1.0.6"
  sha256 "2ec90466d4cc173ccd322816613022cb5900046330518def44fcfe273f6e5911"

  url "https://github.com/edusantosdev/gluco-python/releases/download/v#{version}/Gluco-v#{version}-macos.zip"
  name "Gluco"
  desc "macOS menubar glucose display backed by LibreView"
  homepage "https://github.com/edusantosdev/gluco-python"

  app "Gluco.app"

  zap trash: [
    "~/.glucobar",
    "~/Library/LaunchAgents/com.glucobar.frontend.plist",
  ]
end

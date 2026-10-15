cask "gluco" do
  version "1.0.5"
  sha256 "164717c3df6899ca88971d6adde6deb4f60ee399b51671fb1a83722e83083a96"

  url "https://github.com/edusantosdev/gluco-python/releases/download/v#{version}/Glucobar-v#{version}-macos.zip"
  name "Glucobar"
  desc "macOS menubar glucose display backed by LibreView"
  homepage "https://github.com/edusantosdev/gluco-python"

  app "Glucobar.app"

  zap trash: [
    "~/.glucobar",
    "~/Library/LaunchAgents/com.glucobar.frontend.plist",
  ]
end

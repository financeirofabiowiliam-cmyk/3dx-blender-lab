using System;
using System.Windows.Forms;

namespace AngelsNight.ThreeDX;

internal static class Program
{
    [STAThread]
    static void Main()
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new HybridForm());
    }
}

using TMPro;
using UnityEngine;
using UnityEngine.UI;

public class MatchListItemUI : MonoBehaviour
{
    [Header("Textes")]
    public TMP_Text indexText;
    public TMP_Text playerAText;
    public TMP_Text playerBText;
    public TMP_Text surfaceText;
    public TMP_Text premiumText;
    public TMP_Text oddsText;
    public TMP_Text statusText;

    [Header("Images badges")]
    public Image surfaceBadge;
    public Image premiumBadge;
    public Image oddsBadge;
    public Image statusBadge;

    public void Setup(int index, string playerA, string playerB, string surface, float premiumPct, string veto, string decision, string playerAOdd = "")
    {
        string status = BuildStatus(premiumPct, veto, decision);
        bool nonAnalyzed = IsNonAnalyzedStatus(status);

        if (indexText != null)
            indexText.text = index.ToString();

        if (playerAText != null)
            playerAText.text = playerA;

        if (playerBText != null)
            playerBText.text = "vs " + playerB;

        if (surfaceText != null)
            surfaceText.text = SurfaceToFrench(surface);

        if (premiumText != null)
            premiumText.text = nonAnalyzed ? "N/A" : premiumPct.ToString("0.0") + "%";

        if (oddsText != null)
            oddsText.text = string.IsNullOrWhiteSpace(playerAOdd) ? "COTE -" : "COTE " + playerAOdd;

        if (statusText != null)
            statusText.text = status;

        ApplySurfaceColor(surface);
        ApplyOddsColor();
        ApplyPremiumColor(premiumPct, nonAnalyzed);
        ApplyStatusColor(status);
    }

    private string SurfaceToFrench(string surface)
    {
        string s = (surface ?? "").Trim().ToLower();

        if (s == "clay") return "TERRE";
        if (s == "hard") return "DUR";
        if (s == "grass") return "GAZON";

        return (surface ?? "").ToUpper();
    }

    private string BuildStatus(float premiumPct, string veto, string decision)
    {
        string d = (decision ?? "").Trim().ToLower();
        string v = (veto ?? "").Trim().ToLower();

        // Correction importante : un match non analysé / en erreur ne doit jamais être affiché en REFUSÉ.
        // Exemple backend : decision = "Non analysé" ou message contenant "non analys".
        if (d.Contains("non analys") || d.Contains("non analyze") || d.Contains("not analyzed") || d.Contains("not analysed"))
            return "NON ANALYSÉ";

        if (v == "oui" || v == "yes" || v == "true" || v == "1")
            return "VETO";

        if (premiumPct >= 80f && premiumPct <= 100f)
            return "PREMIUM";

        if (premiumPct >= 75f && premiumPct < 80f)
            return "PROCHE";

        return "REFUSÉ";
    }

    private bool IsNonAnalyzedStatus(string status)
    {
        string s = (status ?? "").Trim().ToUpper();
        return s == "NON ANALYSÉ" || s == "NON ANALYSE";
    }

    private void ApplySurfaceColor(string surface)
    {
        if (surfaceBadge == null) return;

        string s = (surface ?? "").Trim().ToLower();

        if (s == "clay")
            surfaceBadge.color = new Color32(105, 55, 18, 255);      // terre battue
        else if (s == "hard")
            surfaceBadge.color = new Color32(18, 55, 95, 255);       // dur
        else if (s == "grass")
            surfaceBadge.color = new Color32(28, 95, 45, 255);       // gazon
        else
            surfaceBadge.color = new Color32(60, 60, 60, 255);
    }


    private void ApplyOddsColor()
    {
        if (oddsBadge == null) return;

        oddsBadge.color = new Color32(18, 55, 95, 255);
    }

    private void ApplyPremiumColor(float premiumPct, bool nonAnalyzed)
    {
        if (premiumBadge == null) return;

        if (nonAnalyzed)
            premiumBadge.color = new Color32(85, 85, 85, 255);       // gris non analysé
        else if (premiumPct >= 80f)
            premiumBadge.color = new Color32(20, 95, 180, 255);      // bleu premium
        else if (premiumPct >= 75f)
            premiumBadge.color = new Color32(20, 120, 70, 255);      // vert proche
        else
            premiumBadge.color = new Color32(55, 70, 90, 255);       // gris/bleu neutre
    }

    private void ApplyStatusColor(string status)
    {
        if (statusBadge == null) return;

        string s = (status ?? "").Trim().ToUpper();

        if (s == "PREMIUM")
            statusBadge.color = new Color32(20, 95, 180, 255);       // bleu
        else if (s == "PROCHE")
            statusBadge.color = new Color32(20, 120, 70, 255);       // vert
        else if (s == "VETO")
            statusBadge.color = new Color32(150, 85, 20, 255);       // orange
        else if (s == "NON ANALYSÉ" || s == "NON ANALYSE")
            statusBadge.color = new Color32(85, 85, 85, 255);        // gris non analysé
        else
            statusBadge.color = new Color32(140, 45, 45, 255);       // rouge refusé
    }
}
